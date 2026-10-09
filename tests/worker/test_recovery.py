"""Recovery tests: leases, crashes, stale claims (real PostgreSQL)."""

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
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.requests.service import RequestService
from backend.worker.config import WorkerConfig
from backend.worker.outbox_dispatcher import OutboxDispatcher

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_w_recover",
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


async def _claim_row(db, message_id, started):
    """Simulate a crashed worker: claim (+ optionally flag sending)."""
    async with session_scope(db.sessions) as session:
        repo = OutboxRepository(session)
        claimed = await repo.claim_pending(now=NOW, lease_seconds=60.0)
        assert [row.message_id for row in claimed] == [message_id]
        if started:
            assert await repo.set_transmission_started(
                message_id, claimed[0].claim_token)
    return NOW + timedelta(seconds=3600)  # past the 60s lease


@pytest.mark.asyncio
async def test_crash_before_send_permits_recovery(db):  # 24
    keys = _Keys()
    worker = _worker(db, keys)
    sender, records = await _seed(db, keys, 1)
    late = await _claim_row(db, records[0].message_id, started=False)
    recovered = await worker.recover(now=late)
    assert recovered["pending"] == [records[0].message_id]
    assert recovered["uncertain"] == []
    results = await worker.run_once()
    matches = [r for r in results if r.message_id == records[0].message_id]
    assert len(matches) == 1 and matches[0].outcome.value == "SENT"


@pytest.mark.asyncio
async def test_crash_after_possible_send_becomes_uncertain(db):  # 25
    keys = _Keys()
    worker = _worker(db, keys)
    _, records = await _seed(db, keys, 1)
    late = await _claim_row(db, records[0].message_id, started=True)
    recovered = await worker.recover(now=late)
    assert recovered["pending"] == []
    assert recovered["uncertain"] == [records[0].message_id]
    # Never auto-retried: a later run leaves it alone.
    results = await worker.run_once()
    assert all(r.message_id != records[0].message_id for r in results)
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(records[0].message_id)
        assert row is not None and row.status == "UNCERTAIN"


@pytest.mark.asyncio
async def test_expired_lease_recovered_correctly(db):  # 26
    keys = _Keys()
    worker = _worker(db, keys)
    sender, records = await _seed(db, keys, 1)
    message_id = records[0].message_id
    async with session_scope(db.sessions) as session:
        claimed = await OutboxRepository(session).claim_pending(
            now=NOW, lease_seconds=60.0)
        assert message_id in [row.message_id for row in claimed]
        live = await OutboxRepository(session).recover_stale_claims(now=NOW)
        assert live == []  # lease still valid: untouched
        late = await OutboxRepository(session).recover_stale_claims(
            now=NOW + timedelta(seconds=3600))
        assert [row.message_id for row in late] == [message_id]
        assert late[0].status == "PENDING"  # never transmitted
    # Consume the recovered row so later tests see a clean pool.
    await _worker(db, keys).run_once()


@pytest.mark.asyncio
async def test_ambiguous_send_marks_uncertain(db):  # 22
    for behavior in ("timeout", "drop"):
        keys = _Keys()
        worker = _worker(db, keys, behavior=behavior)
        _, records = await _seed(db, keys, 1)
        results = await worker.run_once()
        matches = [r for r in results if r.message_id == records[0].message_id]
        assert matches and matches[0].outcome.value == "UNCERTAIN", behavior
        async with db.sessions() as session:
            row = await OutboxRepository(session).get(records[0].message_id)
            assert row is not None and row.status == "UNCERTAIN"


@pytest.mark.asyncio
async def test_ambiguous_sends_not_blindly_retried(db):  # 23
    keys = _Keys()
    worker = _worker(db, keys, behavior="timeout")
    _, records = await _seed(db, keys, 1)
    await worker.run_once()
    second = await worker.run_once()
    assert all(r.message_id != records[0].message_id for r in second)
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(records[0].message_id)
        assert row is not None and row.attempts == 1
