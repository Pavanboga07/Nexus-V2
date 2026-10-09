"""Request Service integration: real PostgreSQL end-to-end + isolation."""

import asyncio
import ast
import base64
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
from backend.protocol.validation import validate_message
from backend.requests.service import RequestService

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
REQUESTS_ROOT = ROOT / "backend" / "requests"
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test",
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


@pytest.mark.asyncio
async def test_full_request_lifecycle_integration(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    alice, bob = create_identity(), create_identity()
    keys.add(alice)
    keys.add(bob)
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(alice.agent_id, _pub(alice))
        await AgentRepository(session).create(bob.agent_id, _pub(bob))
        await PeerRepository(session).create(
            alice.agent_id, bob.agent_id, status="connected")
    # Create: no Gateway contacted anywhere in this flow.
    record = await service.create_remote_request(
        alice.agent_id, bob.agent_id,
        "What projects are you currently working on?",
        ["memory.read"], EXPIRY, now=NOW,
    )
    assert record.status == "QUEUED"
    # Reload everything fresh and verify end to end.
    async with db.sessions() as session:
        from backend.persistence.repositories.remote_requests import (
            RemoteRequestRepository,
        )

        stored = await RemoteRequestRepository(session).get(record.request_id)
        assert stored is not None and stored.status == "QUEUED"
        stored_message = await MessageRepository(session).get(
            record.message_id)
        assert stored_message is not None
        outbox = await OutboxRepository(session).get(record.message_id)
        assert outbox is not None and outbox.status == "PENDING"
        rebuilt = message_from_dict(
            {**stored_message.envelope,
             "signature": stored_message.signature})
        assert rebuilt.message_id == record.message_id
        assert rebuilt.request_id == record.request_id
        assert rebuilt.signature == stored_message.signature
        assert validate_message(rebuilt, now=NOW) == []
        assert verify_message(rebuilt, alice.public_key, now=NOW) is not None


def _requests_imports():
    modules = set()
    for path in REQUESTS_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
    return modules


def test_no_gateway_implementation_dependency():  # 47
    modules = _requests_imports()
    assert "gateway" not in modules
    assert "websockets" not in modules


def test_no_langgraph_imports():  # 48
    assert "langgraph" not in _requests_imports()


def test_no_langchain_imports():  # 49
    assert "langchain" not in _requests_imports()


def test_no_mcp_imports():  # 50
    assert "mcp" not in _requests_imports()


def test_no_fastapi_imports():  # 51
    assert "fastapi" not in _requests_imports()
    assert "starlette" not in _requests_imports()


def test_no_websocket_imports():  # 52
    assert "websockets" not in _requests_imports()


def test_no_frontend_imports():  # 53
    modules = _requests_imports()
    assert "frontend" not in modules


def test_backend_dependency_direction():
    source_roots = set()
    for path in REQUESTS_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.startswith("backend.")):
                source_roots.add(node.module.split(".")[1])
    assert source_roots <= {"protocol", "identity", "persistence"}, source_roots


@pytest.mark.asyncio
async def test_concurrent_request_creation_with_conflicts(db):  # 43
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender = keys.add(create_identity())
        recipient = keys.add(create_identity())
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")

    async def attempt(suffix):
        return await service.create_remote_request(
            sender.agent_id, recipient.agent_id, f"query {suffix}",
            ["memory.read"], EXPIRY, now=NOW,
        )

    records = await asyncio.gather(*[attempt(i) for i in range(5)])
    assert len({record.request_id for record in records}) == 5
    assert all(record.status == "QUEUED" for record in records)


@pytest.mark.asyncio
async def test_concurrent_state_transitions_legal(db):  # 45
    keys = _Keys()
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
        sender.agent_id, recipient.agent_id, "q?", [], EXPIRY, now=NOW)

    async def mark():
        try:
            await service.mark_sent(record.request_id)
            return "sent"
        except Exception:  # noqa: BLE001 - any failure is a loss
            return "lost"

    results = await asyncio.gather(*[mark() for _ in range(4)])
    assert results.count("sent") == 1
    assert results.count("lost") == 3
    assert (await service.get_request(record.request_id)).status == "SENT"
