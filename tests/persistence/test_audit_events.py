"""Audit-event repository tests (real PostgreSQL)."""

import asyncio
import os
import pathlib
import types
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool

from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.errors import PersistenceError
from backend.persistence.repositories.audit_events import AuditEventRepository

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


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio
async def test_create_audit_event(db):  # 38
    async with session_scope(db.sessions) as session:
        event = await AuditEventRepository(session).create(
            "message.verified", actor_agent_id="agent:a",
            meta={"transport": "gateway"},
        )
        assert event.id is not None
        assert event.event_type == "message.verified"
        assert event.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_query_by_request_id(db):  # 39
    request_id = _tag("req")
    async with session_scope(db.sessions) as session:
        await AuditEventRepository(session).create("a", request_id=request_id)
        await AuditEventRepository(session).create("b", request_id=request_id)
        await AuditEventRepository(session).create("c")
        listed = await AuditEventRepository(session).list_by_request(request_id)
        assert sorted(row.event_type for row in listed) == ["a", "b"]


@pytest.mark.asyncio
async def test_query_by_message_id(db):  # 40
    message_id = _tag("msg")
    async with session_scope(db.sessions) as session:
        await AuditEventRepository(session).create("x", message_id=message_id)
        await AuditEventRepository(session).create("y")
        listed = await AuditEventRepository(session).list_by_message(message_id)
        assert [row.event_type for row in listed] == ["x"]


@pytest.mark.asyncio
async def test_metadata_round_trip(db):  # 41
    meta = {"nested": {"codes": [1, 2, None], "flag": True}, "note": "hi"}
    async with session_scope(db.sessions) as session:
        plain = await AuditEventRepository(session).create("m")
        assert plain.meta is None
        request_id = _tag("req")
        await AuditEventRepository(session).create(
            "m", request_id=request_id, meta=meta
        )
    async with db.sessions() as session:
        listed = await AuditEventRepository(session).list_by_request(request_id)
        assert [row.meta for row in listed] == [meta]


@pytest.mark.asyncio
async def test_malformed_metadata_rejected(db):
    async with db.sessions() as session:
        with pytest.raises(PersistenceError):
            await AuditEventRepository(session).create(
                "m", meta={"bad": {1, 2}}  # type: ignore[dict-item]
            )
