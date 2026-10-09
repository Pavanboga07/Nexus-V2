"""Worker claiming tests: leases, exclusivity, parallelism (real PG)."""

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
from backend.worker.config import LEASE_SAFETY_MARGIN_S, WorkerConfig
from backend.worker.outbox_dispatcher import OutboxDispatcher

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_w_claim",
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
    """Duck-typed GatewayClient: scripted transport behavior."""

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
        raise AssertionError(f"unknown behavior {self.behavior}")

    async def close(self):
        self.closed = True
        self.state = "CLOSED"


def _worker(db, keys, behavior="ack-delivered", service=None, **over):
    fakes = {}

    def factory(sender_id):
        fake = _FakeClient(sender_id, behavior)
        fakes[sender_id] = fake
        return fake

    worker = OutboxDispatcher(
        sessions=db.sessions,
        identities=keys,
        gateway_url="ws://test",
        service=service if service is not None else RequestService(
            db.sessions, keys),
        config=WorkerConfig(**over),
        client_factory=factory,
        clock=lambda: NOW,
        jitter=lambda: 0.5,
    )
    worker.fakes = fakes
    return worker


async def _seed(db, keys, service, count=1):
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
async def test_pending_record_is_claimed(db):  # 12
    keys = _Keys()
    worker = _worker(db, keys)
    sender, records = await _seed(
        db, keys, worker._service, count=1)
    results = await worker.run_once()
    assert len(results) == 1
    assert results[0].outcome.value == "SENT"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(records[0].message_id)
        assert row is not None and row.status == "SENT"
        assert row.attempts == 1
        assert row.claim_token


@pytest.mark.asyncio
async def test_one_record_cannot_be_actively_claimed_twice(db):  # 13
    keys = _Keys()
    first = _worker(db, keys)
    second = _worker(db, keys)
    sender, records = await _seed(db, keys, first._service, count=1)
    first_results, second_results = await asyncio.gather(
        first.run_once(), second.run_once())
    sent = [r for r in first_results + second_results
            if r.outcome.value == "SENT"]
    assert len(sent) == 1
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(records[0].message_id)
        assert row is not None and row.attempts == 1


@pytest.mark.asyncio
async def test_parallel_workers_claim_different_rows(db):  # 14
    keys = _Keys()
    first = _worker(db, keys)
    second = _worker(db, keys)
    _, records = await _seed(db, keys, first._service, count=4)
    first_results, second_results = await asyncio.gather(
        first.run_once(batch_size=10), second.run_once(batch_size=10))
    sent = sorted(
        r.message_id for r in first_results + second_results
        if r.outcome.value == "SENT")
    assert sent == sorted(record.message_id for record in records)
    async with db.sessions() as session:
        for record in records:
            row = await OutboxRepository(session).get(record.message_id)
            assert row is not None and row.attempts == 1


def test_lease_configuration_defaults_valid():
    config = WorkerConfig()
    assert config.lease_seconds > (
        config.handshake_timeout_s + config.ack_timeout_s
        + LEASE_SAFETY_MARGIN_S
    )


def test_unsafe_lease_configuration_rejected():
    with pytest.raises(ValueError, match="lease_seconds"):
        WorkerConfig(lease_seconds=1.0, ack_timeout_s=3600.0)


def test_lease_boundary_requires_strict_excess():
    exact = 10.0 + 30.0 + LEASE_SAFETY_MARGIN_S
    with pytest.raises(ValueError):
        WorkerConfig(lease_seconds=exact)
    ok = WorkerConfig(lease_seconds=exact + 0.001)
    assert ok.lease_seconds == exact + 0.001


def test_handshake_timeout_forwarded_to_clients(db):
    keys = _Keys()
    dispatcher = OutboxDispatcher(
        sessions=db.sessions, identities=keys, gateway_url="ws://test",
        config=WorkerConfig(handshake_timeout_s=7.5),
    )
    sender = keys.add(create_identity())
    client = dispatcher._default_client_factory(sender.agent_id)
    assert client.config.handshake_timeout_s == 7.5
