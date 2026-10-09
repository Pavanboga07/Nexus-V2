"""Peer repository tests (real PostgreSQL)."""

import asyncio
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
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.errors import ConflictError, PersistenceError
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test",
)
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


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


async def _two_agents(session):
    first, second = _agent_key(), _agent_key()
    await AgentRepository(session).create(*first)
    await AgentRepository(session).create(*second)
    return first[0], second[0]


@pytest.mark.asyncio
async def test_create_peer_relation(db):  # 11
    async with session_scope(db.sessions) as session:
        local, peer = await _two_agents(session)
        relation = await PeerRepository(session).create(local, peer)
        assert relation.status == "pending"
        assert relation.revoked_at is None


@pytest.mark.asyncio
async def test_duplicate_peer_relation_rejected(db):  # 12
    async with session_scope(db.sessions) as session:
        local, peer = await _two_agents(session)
        await PeerRepository(session).create(local, peer)
    async with db.sessions() as session:
        with pytest.raises(ConflictError):
            await PeerRepository(session).create(local, peer)


@pytest.mark.asyncio
async def test_lookup_peer(db):  # 13
    async with session_scope(db.sessions) as session:
        local, peer = await _two_agents(session)
        await PeerRepository(session).create(local, peer, status="connected")
        found = await PeerRepository(session).get(local, peer)
        assert found is not None and found.status == "connected"
        assert await PeerRepository(session).get(peer, local) is None
        listed = await PeerRepository(session).list_for_local(local)
        assert [row.peer_agent_id for row in listed] == [peer]


@pytest.mark.asyncio
async def test_revoke_update_peer(db):  # 14
    async with session_scope(db.sessions) as session:
        local, peer = await _two_agents(session)
        await PeerRepository(session).create(local, peer)
        revoked = await PeerRepository(session).revoke(
            local, peer, BASE + timedelta(hours=1)
        )
        assert revoked is not None
        assert revoked.status == "revoked"
        assert revoked.revoked_at is not None


@pytest.mark.asyncio
async def test_transport_connection_not_stored_as_peer(db):  # 15
    from backend.persistence.models import AgentPeer

    columns = set(AgentPeer.__table__.columns.keys())
    assert columns == {
        "id", "local_agent_id", "peer_agent_id", "status",
        "created_at", "updated_at", "revoked_at",
    }
    for name in ("connection", "socket", "transport", "presence"):
        assert name not in columns


@pytest.mark.asyncio
async def test_self_peer_rejected(db):
    async with session_scope(db.sessions) as session:
        agent_id, encoded = _agent_key()
        await AgentRepository(session).create(agent_id, encoded)
        with pytest.raises(PersistenceError):
            await PeerRepository(session).create(agent_id, agent_id)


@pytest.mark.asyncio
async def test_invalid_peer_status_rejected(db):
    async with session_scope(db.sessions) as session:
        local, peer = await _two_agents(session)
        with pytest.raises(PersistenceError):
            await PeerRepository(session).create(local, peer, status="bogus")
