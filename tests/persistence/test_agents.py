"""Agent repository tests (real PostgreSQL)."""

import asyncio
import base64
import os
import pathlib
import types

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
from backend.persistence.errors import (
    ConflictError,
    PersistenceError,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.users import UserRepository

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test",
)


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


def _agent_key():
    identity = create_identity()
    raw = public_key_to_bytes(identity.public_key)
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return identity.agent_id, encoded


@pytest.mark.asyncio
async def test_create_agent(db):  # 7
    agent_id, encoded = _agent_key()
    async with session_scope(db.sessions) as session:
        owner = await UserRepository(session).create()
        agent = await AgentRepository(session).create(
            agent_id, encoded, owner_user_id=owner.id
        )
        assert agent.agent_id == agent_id
        assert agent.active is True
        assert agent.key_algorithm == "Ed25519"


@pytest.mark.asyncio
async def test_agent_owner_relation(db):  # 8
    agent_id, encoded = _agent_key()
    async with session_scope(db.sessions) as session:
        owner = await UserRepository(session).create()
        await AgentRepository(session).create(
            agent_id, encoded, owner_user_id=owner.id
        )
    async with db.sessions() as session:
        agents = await AgentRepository(session).list_by_owner(owner.id)
        assert [agent.agent_id for agent in agents] == [agent_id]


@pytest.mark.asyncio
async def test_duplicate_agent_id_rejected(db):  # 9
    agent_id, encoded = _agent_key()
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(agent_id, encoded)
    async with db.sessions() as session:
        with pytest.raises(ConflictError):
            await AgentRepository(session).create(agent_id, encoded)
    assert issubclass(ConflictError, PersistenceError)


@pytest.mark.asyncio
async def test_agent_deactivation(db):  # 10
    agent_id, encoded = _agent_key()
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(agent_id, encoded)
        assert await AgentRepository(session).deactivate(agent_id) is True
    async with db.sessions() as session:
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None and agent.active is False
        assert await AgentRepository(session).deactivate("no-such-agent") is False


@pytest.mark.asyncio
async def test_oversized_agent_id_rejected(db):
    agent_id, encoded = _agent_key()
    assert len(agent_id) <= 128
    async with db.sessions() as session:
        with pytest.raises(PersistenceError):
            await AgentRepository(session).create("x" * 200, encoded)
