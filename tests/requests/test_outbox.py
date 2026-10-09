"""Outbox repository tests (real PostgreSQL; worker pattern, no worker)."""

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
from backend.persistence.errors import ConflictError
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.requests.errors import RequestConflictError
from backend.requests.service import RequestService

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test",
)
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)


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


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def _drain_board(session):
    """Claim every pending row so claim tests start from a clean pool."""
    repo = OutboxRepository(session)
    while await repo.claim_pending(now=NOW):
        pass


async def _message_row(session, message_id=None):
    sender, recipient = create_identity(), create_identity()
    for ident in (sender, recipient):
        raw = public_key_to_bytes(ident.public_key)
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        await AgentRepository(session).create(ident.agent_id, encoded)
    await PeerRepository(session).create(
        sender.agent_id, recipient.agent_id, status="connected"
    )
    request_id = _tag("req")
    await RemoteRequestRepository(session).create(
        request_id=request_id, sender_agent_id=sender.agent_id,
        recipient_agent_id=recipient.agent_id, query="q",
        requested_capabilities=[], status="QUEUED",
        created_at=BASE, expires_at=BASE + timedelta(hours=1),
    )
    message_id = message_id or _tag("msg")
    await MessageRepository(session).create(
        message_id=message_id, request_id=request_id,
        sender_agent_id=sender.agent_id, recipient_agent_id=recipient.agent_id,
        message_type="REMOTE_QUERY", protocol_version="1.0",
        created_at=BASE, expires_at=BASE + timedelta(hours=1),
        signature="sig", envelope={"type": "REMOTE_QUERY"},
    )
    return message_id


@pytest.mark.asyncio
async def test_outbox_created_with_request_atomically(db):  # 36
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender = keys.add(create_identity())
        recipient = keys.add(create_identity())
        await AgentRepository(session).create(
            sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    record = await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "hi?", [],
        BASE + timedelta(hours=1), now=NOW,
    )
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(record.message_id)
        assert row is not None and row.status == "PENDING"


def _pub(identity):
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.mark.asyncio
async def test_failed_transaction_leaves_no_outbox_record(db):  # 37
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    request_id = _tag("req")
    message_id = _tag("msg")
    await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "hi?", [],
        BASE + timedelta(hours=1), now=NOW, request_id=request_id,
        message_id=message_id,
    )
    with pytest.raises(RequestConflictError):
        await service.create_remote_request(
            sender.agent_id, recipient.agent_id, "hi?", [],
            BASE + timedelta(hours=1), now=NOW, request_id=request_id,
            message_id=_tag("msg"),
        )
    async with db.sessions() as session:
        rows = await OutboxRepository(session).get_pending(now=NOW)
        assert {row.message_id for row in rows} >= {message_id}


async def _pair(session, keys):
    sender, recipient = create_identity(), create_identity()
    keys.add(sender)
    keys.add(recipient)
    await AgentRepository(session).create(sender.agent_id, _pub(sender))
    await AgentRepository(session).create(recipient.agent_id, _pub(recipient))
    await PeerRepository(session).create(
        sender.agent_id, recipient.agent_id, status="connected")
    return sender, recipient


@pytest.mark.asyncio
async def test_pending_lookup_works(db):  # 38
    async with session_scope(db.sessions) as session:
        due = await _message_row(session)
        future = await _message_row(session)
        await OutboxRepository(session).create(due, NOW)
        await OutboxRepository(session).create(
            future, NOW + timedelta(hours=5))
    async with db.sessions() as session:
        pending = await OutboxRepository(session).get_pending(now=NOW)
        ids = {row.message_id for row in pending}
        assert due in ids and future not in ids


@pytest.mark.asyncio
async def test_duplicate_message_id_prevented(db):  # 39
    async with session_scope(db.sessions) as session:
        message_id = await _message_row(session)
        await OutboxRepository(session).create(message_id, NOW)
        with pytest.raises(ConflictError):
            await OutboxRepository(session).create(message_id, NOW)


@pytest.mark.asyncio
async def test_retry_metadata_persists(db):  # 40
    async with session_scope(db.sessions) as session:
        await _drain_board(session)
        message_id = await _message_row(session)
        repo = OutboxRepository(session)
        await repo.create(message_id, NOW)
        claimed = await repo.claim_pending(now=NOW)
        assert [row.message_id for row in claimed] == [message_id]
        assert claimed[0].attempts == 1
        failed = await repo.mark_failed(
            message_id, "x" * 2000, NOW + timedelta(minutes=10))
        assert failed is not None and failed.status == "FAILED"
        assert len(failed.last_error) == 1024  # bounded
        assert failed.available_at == NOW + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_failed_state_persists(db):  # 41
    async with session_scope(db.sessions) as session:
        message_id = await _message_row(session)
        repo = OutboxRepository(session)
        await repo.create(message_id, NOW)
        await repo.mark_failed(message_id, "boom", NOW)
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(message_id)
        assert row is not None and row.status == "FAILED"
        assert row.last_error == "boom"
        assert await OutboxRepository(session).mark_sent(
            "msg_missing", NOW) is None


@pytest.mark.asyncio
async def test_claimed_processing_behavior_safe(db):  # 42, 46
    async with session_scope(db.sessions) as session:
        await _drain_board(session)
        ids = [await _message_row(session) for _ in range(3)]
        repo = OutboxRepository(session)
        for message_id in ids:
            await repo.create(message_id, NOW)

    async def claim_all():
        # Committed claims, like the future worker's: without commit the
        # claim rolls back and a concurrent claimant may re-claim it.
        async with session_scope(db.sessions) as inner:
            return await OutboxRepository(inner).claim_pending(now=NOW)

    first, second, third, fourth = await asyncio.gather(
        claim_all(), claim_all(), claim_all(), claim_all()
    )
    owned = [row.message_id for batch in (first, second, third, fourth)
             for row in batch]
    assert sorted(owned) == sorted(ids)  # each row owned exactly once
    async with db.sessions() as session:
        sent = await OutboxRepository(session).mark_sent(ids[0], NOW)
        assert sent is not None and sent.status == "SENT"
        assert sent.sent_at == NOW


@pytest.mark.asyncio
async def test_claim_carries_token_and_lease(db):
    async with session_scope(db.sessions) as session:
        await _drain_board(session)
        message_id = await _message_row(session)
        repo = OutboxRepository(session)
        await repo.create(message_id, NOW)
        (claimed,) = await repo.claim_pending(now=NOW, lease_seconds=60.0)
        assert claimed.claim_token
        assert claimed.lease_expires_at == NOW + timedelta(seconds=60)
        assert claimed.transmission_started is False


@pytest.mark.asyncio
async def test_conditional_outcome_guards_token(db):
    async with session_scope(db.sessions) as session:
        await _drain_board(session)
        message_id = await _message_row(session)
        repo = OutboxRepository(session)
        await repo.create(message_id, NOW)
        (claimed,) = await repo.claim_pending(now=NOW)
        token = claimed.claim_token
        assert token
        assert await repo.set_transmission_started(
            message_id, "wrong-token") is False
        assert await repo.set_transmission_started(message_id, token) is True
        assert await repo.mark_sent_if_claimed(
            message_id, "wrong-token", NOW) is None
        done = await repo.mark_sent_if_claimed(message_id, token, NOW)
        assert done is not None and done.status == "SENT"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(message_id)
        assert row is not None and row.status == "SENT"


@pytest.mark.asyncio
async def test_recover_stale_claims_branches(db):
    async with session_scope(db.sessions) as session:
        await _drain_board(session)
        unstarted = await _message_row(session)
        started = await _message_row(session)
        repo = OutboxRepository(session)
        await repo.create(unstarted, NOW)
        await repo.create(started, NOW)
        claimed = await repo.claim_pending(now=NOW)
        tokens = {row.message_id: row.claim_token for row in claimed
                  if row.message_id in (unstarted, started)}
        assert await repo.set_transmission_started(
            started, tokens[started]) is True
        late = NOW + timedelta(hours=1)
        recovered = await repo.recover_stale_claims(now=late)
        by_id = {row.message_id: row.status for row in recovered}
        assert by_id.get(unstarted) == "PENDING"
        assert by_id.get(started) == "UNCERTAIN"
    async with db.sessions() as session:
        repo = OutboxRepository(session)
        assert (await repo.get(unstarted)).status == "PENDING"
        assert (await repo.get(started)).status == "UNCERTAIN"
        assert await repo.recover_stale_claims(now=late) == []
