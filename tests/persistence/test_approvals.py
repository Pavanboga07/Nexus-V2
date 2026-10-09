"""Approval repository tests (real PostgreSQL)."""

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
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.errors import ConflictError, PersistenceError
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.approvals import ApprovalRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)

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


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def _request(session):
    agents = []
    for _ in range(2):
        identity = create_identity()
        raw = public_key_to_bytes(identity.public_key)
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        await AgentRepository(session).create(identity.agent_id, encoded)
        agents.append(identity.agent_id)
    return await RemoteRequestRepository(session).create(
        request_id=_tag("req"),
        sender_agent_id=agents[0],
        recipient_agent_id=agents[1],
        query="May I read your calendar?",
        requested_capabilities=["calendar.read"],
        status="WAITING_APPROVAL",
        created_at=BASE,
        expires_at=BASE + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_create_approval(db):  # 30
    async with session_scope(db.sessions) as session:
        request = await _request(session)
        approval = await ApprovalRepository(session).create(
            request.request_id, request.recipient_agent_id, "approved",
            reason="looks fine",
        )
        assert approval.decision == "approved"
        assert approval.reason == "looks fine"


@pytest.mark.asyncio
async def test_retrieve_approval(db):  # 31
    async with session_scope(db.sessions) as session:
        request = await _request(session)
        await ApprovalRepository(session).create(
            request.request_id, request.recipient_agent_id, "rejected"
        )
    async with db.sessions() as session:
        found = await ApprovalRepository(session).get_by_request_id(
            request.request_id
        )
        assert found is not None and found.decision == "rejected"
        assert await ApprovalRepository(session).get_by_request_id(
            "req_missing") is None


@pytest.mark.asyncio
async def test_duplicate_approval_rejected(db):  # 32
    async with session_scope(db.sessions) as session:
        request = await _request(session)
        await ApprovalRepository(session).create(
            request.request_id, request.recipient_agent_id, "approved"
        )
    async with db.sessions() as session:
        with pytest.raises(ConflictError):
            await ApprovalRepository(session).create(
                request.request_id, request.recipient_agent_id, "rejected"
            )


@pytest.mark.asyncio
async def test_request_foreign_key_enforced(db):  # 33
    async with db.sessions() as session:
        with pytest.raises(PersistenceError):
            await ApprovalRepository(session).create(
                "req_never_created", "agent:x", "approved"
            )


@pytest.mark.asyncio
async def test_invalid_decision_rejected(db):
    async with session_scope(db.sessions) as session:
        request = await _request(session)
        with pytest.raises(PersistenceError):
            await ApprovalRepository(session).create(
                request.request_id, request.recipient_agent_id, "maybe"
            )
