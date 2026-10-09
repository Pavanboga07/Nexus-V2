"""Request creation, validation, and atomicity tests (real PostgreSQL)."""

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
from backend.protocol.serialization import message_from_dict
from backend.protocol.validation import validate_message
from backend.identity.verification import verify_message
from backend.persistence.database import (
    create_engine,
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.persistence.repositories.users import UserRepository
from backend.persistence.repositories.users import UserRepository
from backend.requests.errors import (
    InactiveAgentError,
    PeerError,
    RequestConflictError,
    RequestServiceError,
    RequestValidationError,
    UnknownAgentError,
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
    """In-memory identity provider (no globals, no persistence)."""

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


async def _pair(session, keys, peer_status="connected"):
    sender, recipient = create_identity(), create_identity()
    keys.add(sender)
    keys.add(recipient)
    await AgentRepository(session).create(sender.agent_id, _pub(sender))
    await AgentRepository(session).create(recipient.agent_id, _pub(recipient))
    await PeerRepository(session).create(
        sender.agent_id, recipient.agent_id, status=peer_status
    )
    return sender, recipient


def _create(service, sender, recipient, **over):
    fields = {
        "sender_agent_id": sender.agent_id,
        "recipient_agent_id": recipient.agent_id,
        "query": "What projects are you working on?",
        "requested_capabilities": ["memory.read"],
        "expires_at": EXPIRY,
        "now": NOW,
    }
    fields.update(over)
    return service.create_remote_request(**fields)


@pytest.mark.asyncio
async def test_valid_request_creates_successfully(db):  # 7
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    assert record.status == "QUEUED"  # 8
    assert record.request_id.startswith("req_")  # 9
    assert record.message_id is not None and record.message_id.startswith("msg_")  # 10
    assert record.requested_capabilities == ("memory.read",)


@pytest.mark.asyncio
async def test_signed_query_created_and_verifiable(db):  # 11, 12
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    async with db.sessions() as session:
        row = await MessageRepository(session).get(record.message_id)
        assert row is not None
        assert row.message_type == "REMOTE_QUERY"
        assert row.signature
        rebuilt = message_from_dict({**row.envelope, "signature": row.signature})
        assert validate_message(rebuilt, now=NOW) == []
        assert verify_message(rebuilt, sender.public_key, now=NOW).agent_id == (
            sender.agent_id
        )


@pytest.mark.asyncio
async def test_outbox_record_created(db):  # 13
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    async with db.sessions() as session:
        outbox = await OutboxRepository(session).get(record.message_id)
        assert outbox is not None
        assert outbox.status == "PENDING"
        assert outbox.attempts == 0


@pytest.mark.asyncio
async def test_all_three_records_commit_atomically(db):  # 14
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    async with db.sessions() as session:
        assert await RemoteRequestRepository(session).get(record.request_id) is not None
        assert await MessageRepository(session).get(record.message_id) is not None
        assert await OutboxRepository(session).get(record.message_id) is not None


@pytest.mark.asyncio
async def test_unknown_sender_rejected(db):  # 15
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        _, recipient = await _pair(session, keys)
    with pytest.raises(UnknownAgentError):
        await _create(service, _ghost(), recipient)


def _ghost():
    ghost = create_identity()
    return ghost


@pytest.mark.asyncio
async def test_inactive_sender_rejected(db):  # 16
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
        await AgentRepository(session).deactivate(sender.agent_id)
    with pytest.raises(InactiveAgentError):
        await _create(service, sender, recipient)


@pytest.mark.asyncio
async def test_unknown_recipient_rejected(db):  # 17
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, _ = await _pair(session, keys)
    with pytest.raises(UnknownAgentError):
        await _create(service, sender, _ghost())


@pytest.mark.asyncio
async def test_missing_peer_rejected(db):  # 18a
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender = keys.add(create_identity())
        recipient = keys.add(create_identity())
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(recipient.agent_id, _pub(recipient))
    with pytest.raises(PeerError):
        await _create(service, sender, recipient)


@pytest.mark.asyncio
async def test_non_connected_peer_rejected(db):  # 18b
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys, peer_status="pending")
    with pytest.raises(PeerError):
        await _create(service, sender, recipient)


@pytest.mark.asyncio
async def test_invalid_query_rejected(db):  # 19
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    for bad in ("", "   ", 123, None, "x" * 70000):
        with pytest.raises(RequestValidationError):
            await _create(service, sender, recipient, query=bad)


@pytest.mark.asyncio
async def test_invalid_capabilities_rejected(db):  # 20
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    for bad in ("memory.read", ["has space"], [""], ["ok"] * 65, [None]):
        with pytest.raises(RequestValidationError):
            await _create(service, sender, recipient,
                          requested_capabilities=bad)


@pytest.mark.asyncio
async def test_invalid_expiry_rejected(db):  # 21
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    with pytest.raises(RequestValidationError):
        await _create(service, sender, recipient, expires_at=BASE - timedelta(hours=1))
    with pytest.raises(RequestValidationError):
        await _create(service, sender, recipient,
                      expires_at=datetime(2026, 1, 1))  # naive


@pytest.mark.asyncio
async def test_duplicate_request_id_rolls_back_everything(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    first = await _create(service, sender, recipient, request_id=request_id)
    assert first.status == "QUEUED"
    with pytest.raises(RequestConflictError):
        await _create(service, sender, recipient, request_id=request_id,
                      message_id=f"msg_{uuid.uuid4().hex[:12]}")
    async with db.sessions() as session:
        rows = await MessageRepository(session).list_by_request(request_id)
        assert len(rows) == 1  # only the first message; no partial second
        pending = await OutboxRepository(session).get_pending(now=NOW)
        assert first.message_id in {row.message_id for row in pending}


@pytest.mark.asyncio
async def test_duplicate_message_id_rolls_back_request(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    first = await _create(service, sender, recipient, message_id=message_id)
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    with pytest.raises(RequestConflictError):
        await _create(service, sender, recipient, request_id=request_id,
                      message_id=message_id)
    async with db.sessions() as session:
        assert await RemoteRequestRepository(session).get(request_id) is None
        assert (await RemoteRequestRepository(session).get(
            first.request_id)) is not None


@pytest.mark.asyncio
async def test_signing_failure_leaves_no_rows(db):
    keys = _Keys()  # sender key deliberately missing
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender = keys.add(create_identity())  # row needs key? no: row only
        recipient = keys.add(create_identity())
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    keys._keys.pop(sender.agent_id)  # provider cannot sign now
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    with pytest.raises(RequestServiceError):
        await _create(service, sender, recipient, request_id=request_id)
    async with db.sessions() as session:
        assert await RemoteRequestRepository(session).get(request_id) is None


@pytest.mark.asyncio
async def test_explicit_ids_and_conversation_preserved(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
        await UserRepository(session).create()
        from backend.persistence.repositories.conversations import (
            ConversationRepository,
        )
        thread = await ConversationRepository(session).create(
            sender.agent_id, title="t")
    record = await _create(
        service, sender, recipient,
        request_id=f"req_{uuid.uuid4().hex[:12]}",
        message_id=f"msg_{uuid.uuid4().hex[:12]}",
        conversation_id=thread.id,
    )
    assert record.conversation_id == thread.id
    assert record.message_id is not None


@pytest.mark.asyncio
async def test_wrong_signing_key_fails_closed(db):
    from backend.identity.errors import SenderMismatchError

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    intruder = create_identity()
    keys._keys[sender.agent_id] = intruder.private_key  # wrong key, right id
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    with pytest.raises(SenderMismatchError):
        await _create(service, sender, recipient, request_id=request_id)
    async with db.sessions() as session:
        assert await RemoteRequestRepository(session).get(request_id) is None


@pytest.mark.asyncio
async def test_identical_resubmission_conflicts_without_duplication(db):
    # Explicit identifiers are NOT idempotency keys: resubmitting the
    # identical content under the same ids raises a conflict and stores
    # nothing new (documented contract, pinned here).
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    first = await _create(service, sender, recipient, request_id=request_id,
                          message_id=message_id)
    assert first.status == "QUEUED"
    with pytest.raises(RequestConflictError):
        await _create(service, sender, recipient, request_id=request_id,
                      message_id=message_id)
    async with db.sessions() as session:
        rows = await MessageRepository(session).list_by_request(request_id)
        assert [row.message_id for row in rows] == [message_id]
        pending = await OutboxRepository(session).get_pending(now=NOW)
        assert sum(1 for row in pending if row.message_id == message_id) == 1


@pytest.mark.asyncio
async def test_revoked_peer_rejected(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys, peer_status="revoked")
    with pytest.raises(PeerError):
        await _create(service, sender, recipient)


@pytest.mark.asyncio
async def test_complete_unknown_request_rejected(db):
    from backend.requests.errors import UnknownRequestError

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    with pytest.raises(UnknownRequestError):
        await service.complete("req_missing", "msg_missing")


@pytest.mark.asyncio
async def test_full_lifecycle_transitions(db):
    from backend.requests.errors import InvalidStateTransitionError

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    assert (await service.get_request(record.request_id)).status == "QUEUED"
    assert await service.get_request("req_missing") is None
    for step, expect in (
        (service.mark_sent, "SENT"),
        (service.mark_delivered, "DELIVERED"),
        (service.mark_waiting_approval, "WAITING_APPROVAL"),
        (service.mark_processing, "PROCESSING"),
    ):
        if expect == "PROCESSING":
            record = await service.approve(record.request_id, recipient.agent_id)
            assert record.status == "APPROVED"
        record = await step(record.request_id)
        assert record.status == expect


@pytest.mark.asyncio
async def test_illegal_transitions_rejected(db):
    from backend.requests.errors import (
        InvalidStateTransitionError,
        UnknownRequestError,
    )

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    with pytest.raises(InvalidStateTransitionError):
        await service.mark_delivered(record.request_id)  # QUEUED -> DELIVERED
    with pytest.raises(InvalidStateTransitionError):
        await service.approve(record.request_id, recipient.agent_id)
    with pytest.raises(InvalidStateTransitionError):
        await service.complete(record.request_id, "msg_x")
    with pytest.raises(InvalidStateTransitionError):
        await service.expire(record.request_id, now=NOW)  # not yet expired
    cancelled = await service.cancel(record.request_id)
    assert cancelled.status == "CANCELLED"
    for op in (
        lambda: service.mark_sent(record.request_id),
        lambda: service.expire(record.request_id, now=EXPIRY),
        lambda: service.cancel(record.request_id),
        lambda: service.fail(record.request_id, "X", "y"),
    ):
        with pytest.raises(InvalidStateTransitionError):
            await op()
    with pytest.raises(UnknownRequestError):
        await service.mark_sent("req_missing")
    with pytest.raises(UnknownRequestError):
        await service.approve("req_missing", recipient.agent_id)


@pytest.mark.asyncio
async def test_fail_and_expire_paths(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    failed = await _create(service, sender, recipient)
    failed = await service.fail(
        failed.request_id, "E_GATEWAY", "delivery failed")
    assert failed.status == "FAILED"
    assert failed.failure_code == "E_GATEWAY"
    expired = await _create(service, sender, recipient)
    done = await service.expire(expired.request_id, now=EXPIRY)
    assert done.status == "EXPIRED"


@pytest.mark.asyncio
async def test_mark_dispatched_moves_request_and_outbox_together(db):
    from backend.persistence.repositories.outbox import OutboxRepository

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    async with session_scope(db.sessions) as session:
        claimed = await OutboxRepository(session).claim_pending(now=NOW)
        mine = [row for row in claimed
                if row.message_id == record.message_id]
        assert len(mine) == 1
        token = mine[0].claim_token
        assert token
    moved = await service.mark_dispatched(
        record.request_id, record.message_id, claim_token=token, now=NOW)
    assert moved.status == "SENT"
    async with db.sessions() as session:
        row = await OutboxRepository(session).get(record.message_id)
        assert row is not None and row.status == "SENT"
        assert row.sent_at is not None


@pytest.mark.asyncio
async def test_mark_dispatched_rejects_stale_claim(db):
    from backend.requests.errors import RequestServiceError

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    with pytest.raises(RequestServiceError):
        await service.mark_dispatched(
            record.request_id, record.message_id,
            claim_token="stale-token", now=NOW)
    assert (await service.get_request(record.request_id)).status == "QUEUED"


@pytest.mark.asyncio
async def test_mark_dispatched_rejects_wrong_state(db):
    from backend.persistence.repositories.outbox import OutboxRepository
    from backend.requests.errors import InvalidStateTransitionError

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    await service.cancel(record.request_id)
    async with session_scope(db.sessions) as session:
        claimed = await OutboxRepository(session).claim_pending(now=NOW)
        token = claimed[0].claim_token
    with pytest.raises(InvalidStateTransitionError):
        await service.mark_dispatched(
            record.request_id, record.message_id, claim_token=token, now=NOW)


@pytest.mark.asyncio
async def test_database_unavailable_is_retryable():
    from backend.requests.errors import RetryableInfrastructureError

    dead = create_engine("postgresql+asyncpg://u:p@127.0.0.1:59999/db")
    try:
        service = RequestService(create_session_factory(dead), _Keys())
        with pytest.raises(RetryableInfrastructureError) as exc_info:
            await service.get_request("req_x")
        message = str(exc_info.value)
        assert "59999" not in message and "u:p" not in message
        assert exc_info.value.__cause__ is not None
    finally:
        await dispose_engine(dead)


@pytest.mark.asyncio
async def test_conflicts_and_transitions_are_not_retryable(db):
    from backend.requests.errors import (
        InvalidStateTransitionError,
        RetryableInfrastructureError,
    )

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    await _create(service, sender, recipient, request_id=request_id)
    try:
        await _create(service, sender, recipient, request_id=request_id)
        raise AssertionError("duplicate accepted")
    except RequestConflictError as exc:
        assert not isinstance(exc, RetryableInfrastructureError)
    try:
        await service.mark_delivered(request_id)  # QUEUED -> DELIVERED
        raise AssertionError("illegal move accepted")
    except InvalidStateTransitionError as exc:
        assert not isinstance(exc, RetryableInfrastructureError)
