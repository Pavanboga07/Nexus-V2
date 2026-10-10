"""Pairing store: invitation lifecycle and link tracking (real PG)."""

import asyncio
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

from backend.pairing.store import PairingStore
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_pairing",
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
            engine=engine,
            sessions=create_session_factory(engine),
        )
    finally:
        asyncio.run(dispose_engine(engine))


def _now():
    return datetime.now(timezone.utc)


def _invitation_id(prefix):
    import uuid as uuid_mod

    return f"{prefix}-{uuid_mod.uuid4().hex[:8]}"


async def _ensure_agent(db, agent_id):
    import base64

    from backend.identity.identity import create_identity
    from backend.identity.keys import public_key_to_bytes
    from backend.persistence.database import session_scope
    from backend.persistence.repositories.agents import AgentRepository

    identity = create_identity()
    raw = public_key_to_bytes(identity.public_key)
    pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    async with session_scope(db.sessions) as session:
        if await AgentRepository(session).get(agent_id) is None:
            await AgentRepository(session).create(agent_id, pub)


@pytest.mark.asyncio
async def test_invitation_row_state(db):
    await _ensure_agent(db, "agent_b")
    store = PairingStore(db.sessions)
    now = _now()
    invitation_id = _invitation_id("inv")
    await store.create_invitation(
        invitation_id, "agent_b", "hash-1", now + timedelta(hours=1))
    row = await store.get_invitation(invitation_id)
    assert row is not None
    assert row.used_at is None and row.revoked_at is None
    assert await store.get_invitation("missing") is None
    listed = await store.list_invitations("agent_b")
    assert any(r.invitation_id == invitation_id for r in listed)


@pytest.mark.asyncio
async def test_redeem_atomic_single_use_revoke_expiry(db):
    from backend.persistence.database import session_scope

    await _ensure_agent(db, "agent_b")
    store = PairingStore(db.sessions)
    now = _now()
    invitation_id = _invitation_id("inv")
    await store.create_invitation(
        invitation_id, "agent_b", "hash-2", now + timedelta(hours=1))

    async def _redeem(invitation_id, secret, issuer):
        async with session_scope(db.sessions) as session:
            return await store.redeem_for_registration(
                session, invitation_id, secret, issuer, _now())

    # Wrong secret / issuer / id all fail without marking.
    assert await _redeem(invitation_id, "nope", "agent_b") is False
    assert await _redeem(invitation_id, "s", "agent_x") is False
    assert await _redeem("missing", "s", "agent_b") is False
    row = await store.get_invitation(invitation_id)
    assert row.used_at is None

    # Install the real secret hash, then redeem twice: first wins.
    from backend.pairing.invitation import secret_hash

    async with session_scope(db.sessions) as session:
        from backend.persistence.models import PairInvitation
        from sqlalchemy import select

        result = await session.execute(
            select(PairInvitation).where(
                PairInvitation.invitation_id == invitation_id)
        )
        stored = result.scalars().first()
        stored.secret_sha256 = secret_hash("s3cr3t")
    assert await _redeem(invitation_id, "s3cr3t", "agent_b") is True
    assert await _redeem(invitation_id, "s3cr3t", "agent_b") is False
    row = await store.get_invitation(invitation_id)
    assert row.used_at is not None


@pytest.mark.asyncio
async def test_revoked_and_expired_redeem_false(db):
    from backend.persistence.database import session_scope

    await _ensure_agent(db, "agent_b")
    store = PairingStore(db.sessions)
    now = _now()
    revoked_id = _invitation_id("inv")
    await store.create_invitation(
        revoked_id, "agent_b", "hash-3", now + timedelta(hours=1))
    assert await store.revoke_invitation(revoked_id, now) is True
    assert await store.revoke_invitation("missing", now) is False

    async with session_scope(db.sessions) as session:
        assert await store.redeem_for_registration(
            session, revoked_id, "s", "agent_b", now) is False

    expired_id = _invitation_id("inv")
    await store.create_invitation(
        expired_id, "agent_b", "hash-4", now - timedelta(seconds=1))
    async with session_scope(db.sessions) as session:
        assert await store.redeem_for_registration(
            session, expired_id, "s", "agent_b", now) is False


@pytest.mark.asyncio
async def test_link_lifecycle(db):
    await _ensure_agent(db, "agent_a")
    await _ensure_agent(db, "agent_b")
    store = PairingStore(db.sessions)
    link = await store.create_link(
        "agent_a", "agent_b", "pubkey", invitation_id="inv-9",
        request_id="req-9",
    )
    assert link.status == "pending"
    assert await store.find_open_link("agent_a", "agent_b") is not None
    assert await store.set_link_status(link.id, "completed") is True
    assert await store.find_open_link("agent_a", "agent_b") is None
    assert await store.set_link_status(link.id, "completed") is True
    listed = await store.list_links("agent_a")
    assert any(str(row.id) == str(link.id) for row in listed)
