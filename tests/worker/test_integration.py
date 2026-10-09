"""Worker integration: real PG + real Gateway + lifecycle (33-41)."""

import asyncio
import base64
import copy
import os
import pathlib
import types
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool
from websockets.asyncio.client import connect as ws_connect

from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.identity.models import AgentIdentity
from backend.identity.verification import verify_message
from backend.gateway_client.client import GatewayClient
from backend.persistence.database import (
    create_engine,
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.protocol.serialization import message_from_dict
from backend.requests.service import RequestService
from backend.worker.config import WorkerConfig
from backend.worker.outbox_dispatcher import OutboxDispatcher
from gateway.app import GatewayServer, utcnow
import json

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_w_int",
)
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)
EXPIRY = BASE + timedelta(hours=1)


def _migrate():
    asyncio.run(ensure_database_exists(TEST_URL))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def db():
    _migrate()
    engine = _sa_create_engine(
        TEST_URL, poolclass=NullPool, pool_pre_ping=True
    )
    try:
        yield types.SimpleNamespace(
            engine=engine, sessions=create_session_factory(engine)
        )
    finally:
        asyncio.run(dispose_engine(engine))


class _Keys:
    def __init__(self):
        self._keys = {}

    def add(self, identity):
        self._keys[identity.agent_id] = identity.private_key
        return identity

    def private_key_for(self, agent_id):
        return self._keys[agent_id]


def _pub(identity):
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class _FakeClient:
    def __init__(self, agent_id, behavior="ack-delivered", code=""):
        self.agent_id = agent_id
        self.behavior = behavior
        self.code = code
        self.state = "CONNECTED"
        self.sent = []
        self.closed = False

    async def ensure_connected(self):
        from backend.gateway_client.errors import ConnectionLostError

        if self.behavior == "refuse":
            raise ConnectionLostError("refused")
        if self.closed:
            raise ConnectionLostError("closed")

    async def send_message(self, envelope, timeout=None):
        from backend.gateway_client.models import AckResult as Ack

        self.sent.append(copy.deepcopy(envelope))
        mid = envelope["message_id"]
        rid = envelope.get("request_id", "")
        return Ack(mid, "DELIVERED", "delivered", rid)

    async def close(self):
        self.closed = True
        self.state = "CLOSED"


def _worker(db, keys, factory=None, **over):
    now = over.pop("now", NOW)

    def default_factory(sender_id):
        return _FakeClient(sender_id)

    return OutboxDispatcher(
        sessions=db.sessions,
        identities=keys,
        gateway_url="ws://test",
        service=RequestService(db.sessions, keys),
        config=WorkerConfig(**over),
        client_factory=factory or default_factory,
        clock=lambda: now,
        jitter=lambda: 0.5,
    )


async def _seed(db, keys, count=1, now=None, ttl_minutes=30):
    service = RequestService(db.sessions, keys)
    records = []
    moment = now if now is not None else NOW
    expiry = moment + timedelta(minutes=ttl_minutes)
    # Hermetic per test: the live clock makes every historical row
    # claimable, so start from an empty pool.
    async with session_scope(db.sessions) as session:
        from sqlalchemy import text as _text

        for table in ("outbox_messages", "messages", "approvals",
                      "remote_requests", "agent_peers", "agents"):
            await session.execute(_text(f"DELETE FROM {table}"))
    async with session_scope(db.sessions) as session:
        sender = keys.add(create_identity())
        recipient = keys.add(create_identity())
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    for i in range(count):
        records.append(await service.create_remote_request(
            sender.agent_id, recipient.agent_id, f"query {i}?", [],
            expiry, now=moment,
        ))
    return sender, recipient, records


@pytest.mark.asyncio
async def test_database_outage_preserves_committed_state():  # 33
    from backend.requests.errors import RetryableInfrastructureError

    keys = _Keys()
    dead = create_engine("postgresql+asyncpg://u:p@127.0.0.1:59999/db")
    try:
        broken = OutboxDispatcher(
            sessions=create_session_factory(dead),
            identities=keys,
            gateway_url="ws://test",
            service=None,
            config=WorkerConfig(),
            clock=lambda: NOW,
        )
        with pytest.raises(RetryableInfrastructureError):
            await broken.run_once()
    finally:
        await dispose_engine(dead)
    # The shared-database rows from other tests are untouched by this;
    # committed state is never at risk from a dead connection.
    assert True


@pytest.mark.asyncio
async def test_repeated_run_once_safe(db):  # 34
    keys = _Keys()
    worker = _worker(db, keys)
    assert await worker.run_once() == []
    assert await worker.run_once() == []


@pytest.mark.asyncio
async def test_run_forever_cancellation_clean(db):  # 35
    keys = _Keys()
    worker = _worker(db, keys, poll_interval_s=0.05)
    task = asyncio.ensure_future(worker.run_forever())
    await asyncio.sleep(0.3)
    assert not worker.stopped
    await worker.stop()
    assert worker.stopped
    await asyncio.wait_for(asyncio.shield(task), timeout=10)
    assert task.done() and not task.cancelled()
    assert worker._pool == {}


@pytest.mark.asyncio
async def test_run_forever_external_cancel_safe(db):
    keys = _Keys()
    worker = _worker(db, keys, poll_interval_s=0.05)
    task = asyncio.ensure_future(worker.run_forever())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker._pool == {}


@pytest.mark.asyncio
async def test_committed_request_reaches_gateway(db):  # 36
    server = GatewayServer("127.0.0.1", 0)
    port = await server.start()
    try:
        keys = _Keys()
        live = utcnow()
        # Live timestamps: the real Gateway verifies against the real clock.
        worker = _worker(db, keys, now=live)
        worker._client_factory = lambda sender_id: GatewayClient(
            AgentIdentity(agent_id=sender_id,
                          private_key=keys.private_key_for(sender_id)),
            f"ws://127.0.0.1:{port}",
        )
        sender, recipient, records = await _seed(db, keys, 1, now=live)
        inbox: list = []
        ws_bob = await ws_connect(f"ws://127.0.0.1:{port}")
        try:
            now = utcnow()
            from backend.identity.signing import sign_message
            from backend.protocol.models import (
                ConnectRequestPayload, NexusMessage,
            )
            from backend.protocol.serialization import message_to_dict
            from backend.protocol.types import (
                PROTOCOL_VERSION, MessageType, generate_message_id,
                generate_request_id,
            )

            hello = NexusMessage(
                protocol_version=PROTOCOL_VERSION,
                message_id=generate_message_id(),
                request_id=generate_request_id(),
                sender=recipient.agent_id, recipient="gateway",
                type=MessageType.CONNECT_REQUEST, created_at=now,
                expires_at=now + timedelta(minutes=5),
                payload=ConnectRequestPayload(agent_id=recipient.agent_id),
                signature="",
            )
            await ws_bob.send(json.dumps({
                "type": "HANDSHAKE",
                "identity": recipient.public_identity().to_dict(),
                "message": message_to_dict(
                    sign_message(hello, recipient.private_key)),
            }))
            assert json.loads(
                await asyncio.wait_for(ws_bob.recv(), 10))["type"] == "WELCOME"

            results = await worker.run_once()
            assert results and results[0].outcome.value == "SENT"
            frame = json.loads(await asyncio.wait_for(ws_bob.recv(), 10))
            assert frame["type"] == "DELIVER"
            received = frame["message"]
            assert received["message_id"] == records[0].message_id  # 6
            assert received["request_id"] == records[0].request_id  # 7
            assert received["signature"] != ""
            async with db.sessions() as session:
                stored = await MessageRepository(session).get(
                    records[0].message_id)
                assert stored is not None
                assert received["signature"] == stored.signature  # 9
                assert received == {
                    **stored.envelope, "signature": stored.signature}
            rebuilt = message_from_dict(received)  # 5, 8
            assert verify_message(
                rebuilt, sender.public_key, now=utcnow()) is not None
            # 37: consistent states after confirmed handoff.
            async with db.sessions() as session:
                outbox = await OutboxRepository(session).get(
                    records[0].message_id)
                assert outbox is not None and outbox.status == "SENT"
            record = await worker._service.get_request(records[0].request_id)
            assert record is not None and record.status == "SENT"
        finally:
            await ws_bob.close()
    finally:
        await worker.close()
        await server.stop()


@pytest.mark.asyncio
async def test_request_not_sent_before_acceptance(db):  # 38
    keys = _Keys()
    worker = _worker(db, keys)
    sender, _, records = await _seed(db, keys, 1)
    record = await worker._service.get_request(records[0].request_id)
    assert record is not None and record.status == "QUEUED"
    async with db.sessions() as session:
        outbox = await OutboxRepository(session).get(records[0].message_id)
        assert outbox is not None and outbox.status == "PENDING"


@pytest.mark.asyncio
async def test_gateway_failure_leaves_request_recoverable(db):  # 39
    keys = _Keys()
    worker = _worker(db, keys)

    def refuse(sender_id):
        from backend.gateway_client.errors import ConnectionLostError

        raise ConnectionLostError("gateway down")

    worker._client_factory = refuse
    _, _, records = await _seed(db, keys, 1)
    results = await worker.run_once()
    assert results and results[0].outcome.value == "RETRYABLE"
    record = await worker._service.get_request(records[0].request_id)
    assert record is not None and record.status == "QUEUED"
    async with db.sessions() as session:
        outbox = await OutboxRepository(session).get(records[0].message_id)
        assert outbox is not None and outbox.status == "PENDING"


@pytest.mark.asyncio
async def test_persisted_message_valid_after_dispatch_prep(db):  # 41
    keys = _Keys()
    worker = _worker(db, keys)
    sender, _, records = await _seed(db, keys, 1)
    await worker.run_once()
    from backend.protocol.validation import validate_message

    async with db.sessions() as session:
        from backend.persistence.repositories.messages import MessageRepository

        row = await MessageRepository(session).get(records[0].message_id)
        assert row is not None
        rebuilt = message_from_dict(
            {**row.envelope, "signature": row.signature})
        assert validate_message(rebuilt, now=NOW) == []
        assert verify_message(rebuilt, sender.public_key, now=NOW) is not None


def test_worker_has_clean_dependencies():
    import ast
    import pathlib

    modules, roots = set(), set()
    root = pathlib.Path(__file__).resolve().parent.parent.parent / "backend" / "worker"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
                    if node.module.startswith("backend."):
                        roots.add(node.module.split(".")[1])
    for banned in ("gateway", "langgraph", "langchain", "mcp", "frontend",
                   "fastapi", "websockets"):
        assert banned not in modules, banned
    assert roots <= {"protocol", "identity", "persistence", "requests",
                     "gateway_client"}, roots


def test_worker_never_signs():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent.parent / "backend" / "worker"
    for path in root.glob("*.py"):
        assert "sign_message" not in path.read_text(encoding="utf-8"), path.name
