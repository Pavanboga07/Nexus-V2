"""Dispatch outcome tests: SENT, ids, signatures, completion boundary."""

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

from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.identity.verification import verify_message
from backend.persistence.database import (
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

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_w_dispatch",
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
        from backend.gateway_client.errors import AckTimeoutError, ConnectionLostError
        from backend.gateway_client.models import AckResult as Ack

        self.sent.append(copy.deepcopy(envelope))
        mid = envelope["message_id"]
        rid = envelope.get("request_id", "")
        if self.behavior == "ack-delivered":
            return Ack(mid, "DELIVERED", "delivered", rid)
        if self.behavior == "ack-buffered":
            return Ack(mid, "BUFFERED", "buffered", rid)
        if self.behavior == "ack-failed":
            return Ack(mid, "FAILED", f"{self.code}: simulated", rid)
        if self.behavior == "timeout":
            raise AckTimeoutError("no ack")
        if self.behavior == "drop":
            raise ConnectionLostError("lost after send")
        raise AssertionError(self.behavior)

    async def close(self):
        self.closed = True
        self.state = "CLOSED"


def _worker(db, keys, behavior="ack-delivered", **over):
    fakes = {}

    def factory(sender_id):
        fake = _FakeClient(sender_id, behavior)
        fakes[sender_id] = fake
        return fake

    now = over.pop("now", NOW)
    worker = OutboxDispatcher(
        sessions=db.sessions,
        identities=keys,
        gateway_url="ws://test",
        service=RequestService(db.sessions, keys),
        config=WorkerConfig(**over),
        client_factory=factory,
        clock=lambda: now,
        jitter=lambda: 0.5,
    )
    worker.fakes = fakes
    return worker


async def _seed(db, keys, count=1):
    service = RequestService(db.sessions, keys)
    records = []
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
            EXPIRY, now=NOW,
        ))
    return sender, recipient, records


@pytest.mark.asyncio
async def test_success_marks_outbox_sent(db):  # 16
    keys = _Keys()
    worker = _worker(db, keys)
    sender, _, records = await _seed(db, keys, 1)
    results = await worker.run_once()
    assert len(results) == 1 and results[0].outcome.value == "SENT"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(records[0].message_id)
        assert row is not None and row.status == "SENT"
        assert row.sent_at is not None and row.attempts == 1
        assert len(worker.fakes[sender.agent_id].sent) == 1


@pytest.mark.asyncio
async def test_success_updates_request_dispatch_state(db):  # 17
    keys = _Keys()
    worker = _worker(db, keys)
    sender, _, records = await _seed(db, keys, 1)
    await worker.run_once()
    record = await worker._service.get_request(records[0].request_id)
    assert record is not None and record.status == "SENT"


@pytest.mark.asyncio
async def test_gateway_delivery_is_not_completion(db):  # 18
    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-buffered")
    _, _, records = await _seed(db, keys, 1)
    results = await worker.run_once()
    assert results[0].outcome.value == "SENT"
    record = await worker._service.get_request(records[0].request_id)
    assert record is not None and record.status == "SENT"
    assert record.status not in ("DELIVERED", "COMPLETED")


@pytest.mark.asyncio
async def test_retry_preserves_ids(db):  # 31
    keys = _Keys()
    worker = _worker(db, keys, behavior="refuse")
    sender, _, records = await _seed(db, keys, 1)
    first = await worker.run_once()
    assert first[0].outcome.value == "RETRYABLE"
    # Advance past the backoff window, then redeliver with a live gateway.
    later = NOW + timedelta(minutes=5)
    worker2 = _worker(db, keys, behavior="ack-delivered", now=later)
    second = await worker2.run_once()
    # A later clock may also pick up other tests' due rows; only our
    # redelivery is asserted here.
    matches = [r for r in second if r.message_id == records[0].message_id]
    assert len(matches) == 1 and matches[0].outcome.value == "SENT"
    assert matches[0].message_id == records[0].message_id
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(records[0].message_id)
        assert row is not None and row.attempts == 2


@pytest.mark.asyncio
async def test_message_is_not_resigned(db):  # 32
    keys = _Keys()
    worker = _worker(db, keys)
    sender, _, records = await _seed(db, keys, 1)
    await worker.run_once()
    sent = worker.fakes[sender.agent_id].sent[0]
    async with db.sessions() as session:
        row = await MessageRepository(session).get(records[0].message_id)
        assert row is not None
        assert sent["signature"] == row.signature
        rebuilt = message_from_dict(
            {**row.envelope, "signature": row.signature})
        assert verify_message(rebuilt, sender.public_key, now=NOW) is not None
        assert sent == {**row.envelope, "signature": row.signature}
