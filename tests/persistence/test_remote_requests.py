"""Remote-request repository tests (real PostgreSQL)."""

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
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.persistence.repositories.users import UserRepository

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


async def _two_agents(session):
    ids = []
    for _ in range(2):
        identity = create_identity()
        raw = public_key_to_bytes(identity.public_key)
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        await AgentRepository(session).create(identity.agent_id, encoded)
        ids.append(identity.agent_id)
    return ids[0], ids[1]


async def _request(session, sender, recipient, request_id=None, **over):
    fields = {
        "request_id": request_id or _tag("req"),
        "sender_agent_id": sender,
        "recipient_agent_id": recipient,
        "query": "What projects are you working on?",
        "requested_capabilities": ["memory.read"],
        "status": "CREATED",
        "created_at": BASE,
        "expires_at": BASE + timedelta(hours=1),
    }
    fields.update(over)
    return await RemoteRequestRepository(session).create(**fields)


@pytest.mark.asyncio
async def test_create_request(db):  # 16
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request = await _request(session, sender, recipient)
        assert request.request_id.startswith("req_")


@pytest.mark.asyncio
async def test_get_request(db):  # 17
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        created = await _request(session, sender, recipient)
    async with db.sessions() as session:
        found = await RemoteRequestRepository(session).get(created.request_id)
        assert found is not None
        assert found.query.startswith("What projects")
        assert await RemoteRequestRepository(session).get("req_missing") is None


@pytest.mark.asyncio
async def test_list_requests(db):  # 18
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        first = await _request(session, sender, recipient)
        second = await _request(session, sender, recipient)
        by_sender = await RemoteRequestRepository(session).list_by_sender(sender)
        assert {row.request_id for row in by_sender} >= {
            first.request_id, second.request_id}
        by_recipient = await RemoteRequestRepository(session).list_by_recipient(
            recipient)
        assert {row.request_id for row in by_recipient} >= {
            first.request_id, second.request_id}
        by_status = await RemoteRequestRepository(session).list_by_status("CREATED")
        assert {row.request_id for row in by_status} >= {
            first.request_id, second.request_id}


@pytest.mark.asyncio
async def test_update_stored_fields(db):  # 19
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        created = await _request(session, sender, recipient)
        updated = await RemoteRequestRepository(session).update(
            created.request_id, status="DELIVERED"
        )
        assert updated is not None and updated.status == "DELIVERED"
        stamped = await RemoteRequestRepository(session).save_response_reference(
            created.request_id, "msg_resp_1", BASE + timedelta(minutes=5)
        )
        assert stamped is not None
        assert stamped.response_message_id == "msg_resp_1"
        assert stamped.completed_at is not None


@pytest.mark.asyncio
async def test_duplicate_request_id_rejected(db):  # 20
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request_id = _tag("req")
        await _request(session, sender, recipient, request_id)
    async with db.sessions() as session:
        with pytest.raises(ConflictError):
            await _request(session, sender, recipient, request_id)


@pytest.mark.asyncio
async def test_request_agent_foreign_keys_enforced(db):  # 21
    async with session_scope(db.sessions) as session:
        sender, _ = await _two_agents(session)
        with pytest.raises(PersistenceError):
            await _request(session, sender, "agent:never-created")


@pytest.mark.asyncio
async def test_json_capabilities_round_trip(db):  # 22
    caps = ["memory.read", {"nested": ["calendar.read", 1, None]}]
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        created = await _request(
            session, sender, recipient, requested_capabilities=caps
        )
    async with db.sessions() as session:
        found = await RemoteRequestRepository(session).get(created.request_id)
        assert found is not None and found.requested_capabilities == caps


@pytest.mark.asyncio
async def test_timezone_timestamps_round_trip(db):  # 23
    plus5 = timezone(timedelta(hours=5))
    created_at = datetime(2026, 1, 1, 17, 0, tzinfo=plus5)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        created = await _request(
            session, sender, recipient, created_at=created_at,
            expires_at=created_at + timedelta(hours=1),
        )
    async with db.sessions() as session:
        found = await RemoteRequestRepository(session).get(created.request_id)
        assert found is not None
        assert found.created_at.tzinfo is not None
        assert found.created_at == created_at


@pytest.mark.asyncio
async def test_rollback_leaves_no_partial_writes(db):  # 44
    from backend.persistence.repositories.messages import MessageRepository

    async with db.sessions() as session:
        owner = await UserRepository(session).create()
        sender, recipient = await _two_agents(session)
        request = await _request(session, sender, recipient)
        await MessageRepository(session).create(
            message_id=_tag("msg"), request_id=request.request_id,
            sender_agent_id=sender, recipient_agent_id=recipient,
            message_type="REMOTE_QUERY", protocol_version="1.0",
            created_at=BASE, expires_at=BASE + timedelta(hours=1),
            signature="sig", envelope={"type": "REMOTE_QUERY"},
        )
        await session.rollback()
    async with db.sessions() as session:
        assert await RemoteRequestRepository(session).get(request.request_id) is None
        assert owner is not None  # owner id captured pre-rollback for lookup below
        assert await UserRepository(session).get(owner.id) is None


@pytest.mark.asyncio
async def test_concurrent_conflicting_insert_safe(db):  # 45
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
    request_id = _tag("req")

    async def attempt():
        async with db.sessions() as session:
            try:
                await _request(session, sender, recipient, request_id)
                await session.commit()
                return "ok"
            except ConflictError:
                await session.rollback()
                return "conflict"

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == ["conflict", "ok"]
    async with db.sessions() as session:
        assert await RemoteRequestRepository(session).get(request_id) is not None
