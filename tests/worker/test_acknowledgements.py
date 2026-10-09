"""ACK classification tests: handoff, rejection, replay (fake transports)."""

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
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.requests.service import RequestService
from backend.worker.config import WorkerConfig
from backend.worker.outbox_dispatcher import OutboxDispatcher

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_w_ack",
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
        if self.behavior == "ack-delivered":
            return Ack(mid, "DELIVERED", "delivered", rid)
        if self.behavior == "ack-buffered":
            return Ack(mid, "BUFFERED", "stored for reconnect", rid)
        if self.behavior == "ack-failed":
            return Ack(mid, "FAILED", f"{self.code}: simulated", rid)
        raise AssertionError(self.behavior)

    async def close(self):
        self.closed = True
        self.state = "CLOSED"


def _worker(db, keys, behavior="ack-delivered", code=""):
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
        config=WorkerConfig(),
        client_factory=factory,
        clock=lambda: NOW,
        jitter=lambda: 0.5,
    )
    worker.fakes = fakes
    return worker


async def _seed(db, keys):
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender = keys.add(create_identity())
        recipient = keys.add(create_identity())
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    record = await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "query?", [],
        EXPIRY, now=NOW,
    )
    return sender, record


@pytest.mark.asyncio
async def test_buffered_ack_confirms_handoff(db):
    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-buffered")
    _, record = await _seed(db, keys)
    results = await worker.run_once()
    assert results[0].outcome.value == "SENT"
    assert results[0].detail == "GATEWAY_BUFFERED"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(record.message_id)
        assert row is not None and row.status == "SENT"
    assert (await worker._service.get_request(
        record.request_id)).status == "SENT"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    "INVALID_MESSAGE",
    "INVALID_SIGNATURE",
    "SENDER_MISMATCH",
    "UNKNOWN_RECIPIENT",
    "MESSAGE_TOO_LARGE",
])
async def test_permanent_ack_codes_fail(db, code):
    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-failed", code=code)
    _, record = await _seed(db, keys)
    results = await worker.run_once()
    assert results[0].outcome.value == "PERMANENT"
    assert results[0].detail == f"GATEWAY_{code}"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(record.message_id)
        assert row is not None and row.status == "FAILED"
    assert (await worker._service.get_request(
        record.request_id)).status == "FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["RATE_LIMITED", "BUFFER_FULL"])
async def test_retryable_ack_codes_reschedule(db, code):
    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-failed", code=code)
    _, record = await _seed(db, keys)
    results = await worker.run_once()
    assert results[0].outcome.value == "RETRYABLE"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(record.message_id)
        assert row is not None and row.status == "PENDING"
        assert row.available_at > NOW


@pytest.mark.asyncio
async def test_replayed_ack_becomes_uncertain(db):
    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-failed", code="REPLAYED")
    _, record = await _seed(db, keys)
    results = await worker.run_once()
    assert results[0].outcome.value == "UNCERTAIN"
    assert results[0].detail == "GATEWAY_REPLAYED"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(record.message_id)
        assert row is not None and row.status == "UNCERTAIN"


@pytest.mark.asyncio
async def test_unknown_ack_code_becomes_uncertain(db):
    keys = _Keys()
    worker = _worker(db, keys, behavior="ack-failed", code="FROBNICATED")
    _, record = await _seed(db, keys)
    results = await worker.run_once()
    assert results[0].outcome.value == "UNCERTAIN"
    assert "FROBNICATED" in results[0].detail
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(record.message_id)
        assert row is not None and row.status == "UNCERTAIN"
