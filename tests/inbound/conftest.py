"""Shared fixtures and builders for inbound Component 9 tests (real PG)."""

from __future__ import annotations

import asyncio
import base64
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
from backend.identity.signing import sign_message
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.requests.message_builder import build_remote_query_message

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
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


def _pub(identity):
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unique(prefix):
    return f"{prefix}9_{uuid.uuid4().hex}"


async def seed_recipient_side(db, sender, recipient):
    """Create agent rows plus a connected (recipient -> sender) peer."""
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient)
        )
        await PeerRepository(session).create(
            recipient.agent_id, sender.agent_id, status="connected"
        )


def signed_query(
    sender,
    recipient,
    query="May I read your calendar?",
    capabilities=("calendar.read",),
    request_id=None,
    message_id=None,
    created_at=None,
    expires_at=None,
):
    """Build and sign a REMOTE_QUERY exactly like the outbound service."""
    unsigned = build_remote_query_message(
        sender_agent_id=sender.agent_id,
        recipient_agent_id=recipient.agent_id,
        request_id=request_id or unique("req"),
        message_id=message_id or unique("msg"),
        query=query,
        requested_capabilities=list(capabilities),
        created_at=created_at or NOW,
        expires_at=expires_at if expires_at is not None else EXPIRY,
    )
    return sign_message(unsigned, sender.private_key)


def live_query(sender, recipient, **kwargs):
    """A signed query valid against the real wall clock (dispatch tests)."""
    created = datetime.now(timezone.utc)
    kwargs.setdefault("created_at", created)
    kwargs.setdefault("expires_at", created + timedelta(hours=1))
    return signed_query(sender, recipient, **kwargs)
