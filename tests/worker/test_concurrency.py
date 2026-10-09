"""Worker concurrency tests: pool dedup, cross-worker claims (real PG)."""

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
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_w_conc",
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
    def __init__(self, agent_id, creations):
        self.agent_id = agent_id
        self.state = "CONNECTED"
        self.sent = []
        self.closed = False
        self._creations = creations

    async def ensure_connected(self):
        pass

    async def send_message(self, envelope, timeout=None):
        from backend.gateway_client.models import AckResult as Ack

        self.sent.append(copy.deepcopy(envelope))
        return Ack(envelope["message_id"], "DELIVERED", "ok",
                   envelope.get("request_id", ""))

    async def close(self):
        self.closed = True
        self.state = "CLOSED"


def _counting_worker(db, keys, creations):
    def factory(sender_id):
        creations[sender_id] = creations.get(sender_id, 0) + 1
        return _FakeClient(sender_id, creations)

    return OutboxDispatcher(
        sessions=db.sessions,
        identities=keys,
        gateway_url="ws://test",
        service=RequestService(db.sessions, keys),
        config=WorkerConfig(),
        client_factory=factory,
        clock=lambda: NOW,
        jitter=lambda: 0.5,
    )


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
    return records


@pytest.mark.asyncio
async def test_pool_creates_one_connection_per_agent(db):  # 40-adjacent
    keys = _Keys()
    creations: dict = {}
    worker = _counting_worker(db, keys, creations)
    await _seed(db, keys, 3)

    async def hammer():
        await worker._client_for(
            list(keys._keys)[0])

    await asyncio.gather(*[hammer() for _ in range(10)])
    agent_id = list(keys._keys)[0]
    assert creations.get(agent_id, 0) == 1


@pytest.mark.asyncio
async def test_concurrent_dispatchers_do_not_duplicate_claims(db):  # 40
    keys = _Keys()
    first = _counting_worker(db, keys, {})
    second = _counting_worker(db, keys, {})
    records = await _seed(db, keys, 4)
    first_results, second_results = await asyncio.gather(
        first.run_once(), second.run_once())
    sent = sorted(
        r.message_id for r in first_results + second_results
        if r.outcome.value == "SENT")
    assert sent == sorted(record.message_id for record in records)
    async with db.sessions() as session:
        for record in records:
            row = await OutboxRepository(session).get(record.message_id)
            assert row is not None and row.attempts == 1
