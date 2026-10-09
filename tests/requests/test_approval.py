"""Approval operation tests (real PostgreSQL, incl. races)."""

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
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.approvals import ApprovalRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.requests.errors import (
    ApprovalError,
    InvalidStateTransitionError,
)
from backend.requests.service import RequestService

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


async def _waiting(keys, service, db):
    async with session_scope(db.sessions) as session:
        sender, recipient = create_identity(), create_identity()
        keys.add(sender)
        keys.add(recipient)
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    record = await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "May I read your calendar?",
        ["calendar.read"], EXPIRY, now=NOW,
    )
    record = await service.mark_sent(record.request_id)
    record = await service.mark_delivered(record.request_id)
    record = await service.mark_waiting_approval(record.request_id)
    assert record.status == "WAITING_APPROVAL"
    return sender, recipient, record


@pytest.mark.asyncio
async def test_valid_recipient_can_approve(db):  # 22
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _waiting(keys, service, db)
    approved = await service.approve(
        record.request_id, recipient.agent_id, reason="yes, shared")
    assert approved.status == "APPROVED"
    async with db.sessions() as session:
        row = await ApprovalRepository(session).get_by_request_id(
            record.request_id)
        assert row is not None and row.decision == "approved"


@pytest.mark.asyncio
async def test_non_recipient_cannot_approve(db):  # 23
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _waiting(keys, service, db)
        intruder = keys.add(create_identity())
        await AgentRepository(session).create(intruder.agent_id, _pub(intruder))
    with pytest.raises(ApprovalError):
        await service.approve(record.request_id, intruder.agent_id)
    with pytest.raises(ApprovalError):
        await service.approve(record.request_id, sender.agent_id)


@pytest.mark.asyncio
async def test_duplicate_approval_rejected(db):  # 24
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        _, recipient, record = await _waiting(keys, service, db)
    await service.approve(record.request_id, recipient.agent_id)
    with pytest.raises((ApprovalError, InvalidStateTransitionError)):
        await service.approve(record.request_id, recipient.agent_id)


@pytest.mark.asyncio
async def test_approval_and_state_transition_atomic(db):  # 25
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        _, recipient, record = await _waiting(keys, service, db)
    await service.approve(record.request_id, recipient.agent_id)
    async with db.sessions() as session:
        row = await RemoteRequestRepository(session).get(record.request_id)
        assert row is not None and row.status == "APPROVED"
        decision = await ApprovalRepository(session).get_by_request_id(
            record.request_id)
        assert decision is not None and decision.decision == "approved"


@pytest.mark.asyncio
async def test_rejection_records_reason(db):  # 26
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        _, recipient, record = await _waiting(keys, service, db)
    rejected = await service.reject(
        record.request_id, recipient.agent_id, reason="too broad")
    assert rejected.status == "REJECTED"
    async with db.sessions() as session:
        row = await ApprovalRepository(session).get_by_request_id(
            record.request_id)
        assert row is not None
        assert row.decision == "rejected" and row.reason == "too broad"


@pytest.mark.asyncio
async def test_reject_approve_race_resolves_safely(db):  # 27 + 4
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        _, recipient, record = await _waiting(keys, service, db)

    async def do_approve():
        try:
            await service.approve(record.request_id, recipient.agent_id)
            return "approved"
        except (ApprovalError, InvalidStateTransitionError):
            return "lost"

    async def do_reject():
        try:
            await service.reject(
                record.request_id, recipient.agent_id, reason="no")
            return "rejected"
        except (ApprovalError, InvalidStateTransitionError):
            return "lost"

    first, second = await asyncio.gather(do_approve(), do_reject())
    assert sorted([first, second]) in (
        ["approved", "lost"], ["lost", "rejected"])
    async with db.sessions() as session:
        row = await RemoteRequestRepository(session).get(record.request_id)
        assert row is not None and row.status in ("APPROVED", "REJECTED")
        decision = await ApprovalRepository(session).get_by_request_id(
            record.request_id)
        assert decision is not None
        assert decision.decision == row.status.lower()
