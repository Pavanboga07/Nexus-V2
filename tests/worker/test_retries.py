"""Retry, backoff, and permanent-failure tests (fake transports)."""

import asyncio
import base64
import copy
import os
import pathlib
import types
import uuid
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
from backend.requests.service import RequestService
from backend.worker.config import WorkerConfig
from backend.worker.models import compute_backoff
from backend.worker.outbox_dispatcher import OutboxDispatcher

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_w_retry",
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


def _worker(db, keys, behavior="ack-delivered", code="", **over):
    fakes = {}

    def factory(sender_id):
        fake = _FakeClient(sender_id, behavior, code)
        fakes[sender_id] = fake
        return fake

    worker = OutboxDispatcher(
        sessions=db.sessions,
        identities=keys,
        gateway_url="ws://test",
        service=RequestService(db.sessions, keys),
        config=WorkerConfig(**over),
        client_factory=factory,
        clock=lambda: NOW,
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
    return sender, records


@pytest.mark.asyncio
async def test_retryable_presend_failure_reschedules(db):  # 19
    keys = _Keys()
    worker = _worker(db, keys, behavior="refuse")
    sender, records = await _seed(db, keys, 1)
    results = await worker.run_once()
    assert results[0].outcome.value == "RETRYABLE"
    assert results[0].next_available_at is not None
    assert results[0].next_available_at > NOW
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(records[0].message_id)
        assert row is not None and row.status == "PENDING"
        assert row.attempts == 1
    record = await worker._service.get_request(records[0].request_id)
    assert record is not None and record.status == "QUEUED"


@pytest.mark.asyncio
async def test_exponential_backoff_bounded():  # 20
    assert compute_backoff(1, 1.0, 300.0, 0.0) == 0.5
    assert compute_backoff(1, 1.0, 300.0, 1.0) == 1.0
    assert compute_backoff(4, 1.0, 300.0, 1.0) == 8.0
    assert compute_backoff(20, 1.0, 300.0, 1.0) == 300.0
    assert compute_backoff(20, 1.0, 300.0, 0.0) == 150.0
    for attempt in range(1, 12):
        value = compute_backoff(attempt, 2.0, 60.0, 0.37)
        assert 1.0 <= value <= 60.0


@pytest.mark.asyncio
async def test_permanent_failure_marks_failed(db):  # 21
    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-failed", code="INVALID_SIGNATURE")
    sender, records = await _seed(db, keys, 1)
    results = await worker.run_once()
    assert results[0].outcome.value == "PERMANENT"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(records[0].message_id)
        assert row is not None and row.status == "FAILED"
        assert "INVALID_SIGNATURE" in (row.last_error or "")
    record = await worker._service.get_request(records[0].request_id)
    assert record is not None and record.status == "FAILED"


@pytest.mark.asyncio
async def test_expired_message_not_dispatched(db):  # 29
    # Authority for expiry lives inside the signed envelope, not the row
    # metadata: build an envelope that is already past at dispatch time.
    from backend.identity.signing import sign_message
    from backend.protocol.models import NexusMessage, RemoteQueryPayload
    from backend.protocol.serialization import message_to_dict
    from backend.protocol.types import PROTOCOL_VERSION, MessageType

    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-delivered")
    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    old = BASE - timedelta(hours=2)
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=f"msg_{uuid.uuid4().hex[:12]}",
        request_id=f"req_{uuid.uuid4().hex[:12]}",
        sender=sender.agent_id,
        recipient=recipient.agent_id,
        type=MessageType.REMOTE_QUERY,
        created_at=old,
        expires_at=old + timedelta(minutes=30),
        payload=RemoteQueryPayload(query="stale?", requested_capabilities=[]),
        signature="",
    )
    signed = sign_message(message, sender.private_key)
    wire = message_to_dict(signed)
    async with session_scope(db.sessions) as session:
        from backend.persistence.repositories.remote_requests import (
            RemoteRequestRepository,
        )
        from backend.persistence.repositories.messages import MessageRepository
        from backend.persistence.repositories.outbox import OutboxRepository

        await RemoteRequestRepository(session).create(
            request_id=signed.request_id, sender_agent_id=sender.agent_id,
            recipient_agent_id=recipient.agent_id, query="stale?",
            requested_capabilities=[], status="QUEUED",
            created_at=old, expires_at=old + timedelta(minutes=30),
        )
        await MessageRepository(session).create(
            message_id=signed.message_id, request_id=signed.request_id,
            sender_agent_id=signed.sender,
            recipient_agent_id=signed.recipient,
            message_type="REMOTE_QUERY", protocol_version="1.0",
            created_at=signed.created_at, expires_at=signed.expires_at,
            signature=signed.signature,
            envelope={k: v for k, v in wire.items() if k != "signature"},
        )
        await OutboxRepository(session).create(signed.message_id, NOW)
    results = await worker.run_once()
    matches = [r for r in results if r.message_id == signed.message_id]
    assert matches and matches[0].outcome.value == "PERMANENT"
    # Rejected before any transport existed: nothing was ever sent.
    assert sender.agent_id not in worker.fakes


@pytest.mark.asyncio
async def test_invalid_signed_message_not_dispatched(db):  # 30
    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-delivered")
    sender, records = await _seed(db, keys, 1)
    async with session_scope(db.sessions) as session:
        row = await MessageRepository(session).get(records[0].message_id)
        assert row is not None
        row.signature = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    results = await worker.run_once()
    matches = [r for r in results if r.message_id == records[0].message_id]
    assert matches and matches[0].outcome.value == "PERMANENT"
    # No transport was ever created: rejection precedes any send attempt.
    assert sender.agent_id not in worker.fakes
