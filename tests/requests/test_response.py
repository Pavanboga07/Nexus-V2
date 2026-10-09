"""Response-correlation tests (real PostgreSQL + real signatures)."""

import asyncio
import base64
import copy
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
from backend.identity.verification import verify_message
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.protocol.models import NexusMessage, RemoteResponsePayload
from backend.protocol.serialization import message_from_dict, message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import validate_message
from backend.requests.errors import ResponseCorrelationError
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


async def _pair(session, keys):
    from backend.persistence.repositories.peers import PeerRepository

    sender, recipient = create_identity(), create_identity()
    keys.add(sender)
    keys.add(recipient)
    await AgentRepository(session).create(sender.agent_id, _pub(sender))
    await AgentRepository(session).create(recipient.agent_id, _pub(recipient))
    await PeerRepository(session).create(
        sender.agent_id, recipient.agent_id, status="connected")
    return sender, recipient


async def _create(service, sender, recipient):
    return await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "q?", [], EXPIRY, now=NOW)


async def _processing(keys, service, db):
    async with session_scope(db.sessions) as session:
        sender, recipient = create_identity(), create_identity()
        keys.add(sender)
        keys.add(recipient)
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    record = await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "Summarize this?",
        ["memory.read"], EXPIRY, now=NOW,
    )
    for step in (service.mark_sent, service.mark_delivered,
                 service.mark_waiting_approval):
        record = await step(record.request_id)
    record = await service.approve(record.request_id, recipient.agent_id)
    record = await service.mark_processing(record.request_id)
    assert record.status == "PROCESSING"
    return sender, recipient, record


def _response_envelope(responder, requester_id, request_id, **over):
    payload = RemoteResponsePayload(status="ok", answer="Done.")
    payload_fields = dict(over.pop("payload_over", {}))
    for name, value in payload_fields.items():
        setattr(payload, name, value)
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=over.pop("message_id", f"msg_{uuid.uuid4().hex[:12]}"),
        request_id=over.pop("request_ref", request_id),
        sender=responder.agent_id,
        recipient=over.pop("recipient_id", requester_id),
        type=MessageType.REMOTE_RESPONSE,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        payload=payload,
        signature="",
    )
    return message_to_dict(sign_message(message, responder.private_key))


@pytest.mark.asyncio
async def test_valid_response_completes_request(db):  # 28
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
    envelope = _response_envelope(recipient, sender.agent_id, record.request_id)
    done = await service.handle_remote_response(
        envelope, sender.agent_id, now=NOW)
    assert done.status == "COMPLETED"  # 28
    assert done.response_message_id == envelope["message_id"]  # 30
    async with db.sessions() as session:
        row = await MessageRepository(session).get(envelope["message_id"])
        assert row is not None  # 29


@pytest.mark.asyncio
async def test_wrong_request_id_rejected(db):  # 31
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
    envelope = _response_envelope(recipient, sender.agent_id, "req_nope")
    with pytest.raises(ResponseCorrelationError):
        await service.handle_remote_response(envelope, sender.agent_id, now=NOW)


@pytest.mark.asyncio
async def test_wrong_recipient_rejected(db):  # 32
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
        stranger = keys.add(create_identity())
        await AgentRepository(session).create(stranger.agent_id, _pub(stranger))
    envelope = _response_envelope(
        recipient, stranger.agent_id, record.request_id)
    with pytest.raises(ResponseCorrelationError):
        await service.handle_remote_response(
            envelope, stranger.agent_id, now=NOW)


@pytest.mark.asyncio
async def test_unrelated_response_cannot_complete_request(db):  # 33
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
        other = keys.add(create_identity())
        await AgentRepository(session).create(other.agent_id, _pub(other))
    envelope = _response_envelope(other, sender.agent_id, record.request_id)
    with pytest.raises(ResponseCorrelationError):
        await service.handle_remote_response(envelope, sender.agent_id, now=NOW)


@pytest.mark.asyncio
async def test_late_response_handled_deterministically(db):  # 34
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
    first = _response_envelope(recipient, sender.agent_id, record.request_id)
    await service.handle_remote_response(first, sender.agent_id, now=NOW)
    second = _response_envelope(recipient, sender.agent_id, record.request_id)
    with pytest.raises(ResponseCorrelationError):
        await service.handle_remote_response(second, sender.agent_id, now=NOW)
    async with db.sessions() as session:
        from backend.persistence.repositories.remote_requests import (
            RemoteRequestRepository,
        )

        row = await RemoteRequestRepository(session).get(record.request_id)
        assert row is not None and row.status == "COMPLETED"


@pytest.mark.asyncio
async def test_response_verification_valid_after_persistence(db):  # 35
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
    envelope = _response_envelope(recipient, sender.agent_id, record.request_id)
    await service.handle_remote_response(envelope, sender.agent_id, now=NOW)
    async with db.sessions() as session:
        row = await MessageRepository(session).get(envelope["message_id"])
        assert row is not None
        rebuilt = message_from_dict(
            {**row.envelope, "signature": row.signature})
        assert validate_message(rebuilt, now=NOW) == []
        assert verify_message(rebuilt, recipient.public_key, now=NOW) is not None


@pytest.mark.asyncio
async def test_error_response_fails_request(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
    envelope = _response_envelope(
        recipient, sender.agent_id, record.request_id,
        payload_over={"status": "error", "answer": "",
                      "error_code": "E_TOOL", "error_message": "tool broke"},
    )
    done = await service.handle_remote_response(
        envelope, sender.agent_id, now=NOW)
    assert done.status == "FAILED"
    assert done.failure_code == "E_TOOL"
    assert done.failure_message == "tool broke"


@pytest.mark.asyncio
async def test_premature_response_rejected(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = create_identity(), create_identity()
        keys.add(sender)
        keys.add(recipient)
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    record = await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "q?", [], EXPIRY, now=NOW)
    envelope = _response_envelope(recipient, sender.agent_id, record.request_id)
    with pytest.raises(ResponseCorrelationError):
        await service.handle_remote_response(envelope, sender.agent_id, now=NOW)


@pytest.mark.asyncio
async def test_tampered_response_rejected(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
    envelope = _response_envelope(recipient, sender.agent_id, record.request_id)
    envelope["payload"]["answer"] = "forged"
    with pytest.raises(ResponseCorrelationError):
        await service.handle_remote_response(envelope, sender.agent_id, now=NOW)


@pytest.mark.asyncio
async def test_concurrent_responses_have_one_winner(db):
    import copy

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
    envelope = _response_envelope(recipient, sender.agent_id, record.request_id)

    async def attempt():
        try:
            await service.handle_remote_response(
                copy.deepcopy(envelope), sender.agent_id, now=NOW)
            return "completed"
        except ResponseCorrelationError:
            return "rejected"

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == ["completed", "rejected"]
    async with db.sessions() as session:
        from backend.persistence.repositories.remote_requests import (
            RemoteRequestRepository,
        )

        row = await RemoteRequestRepository(session).get(record.request_id)
        assert row is not None and row.status == "COMPLETED"


@pytest.mark.asyncio
async def test_wrong_agent_response_leaves_state_untouched(db):
    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient, record = await _processing(keys, service, db)
        other = keys.add(create_identity())
        await AgentRepository(session).create(other.agent_id, _pub(other))
    envelope = _response_envelope(other, sender.agent_id, record.request_id)
    with pytest.raises(ResponseCorrelationError):
        await service.handle_remote_response(envelope, sender.agent_id, now=NOW)
    async with db.sessions() as session:
        from backend.persistence.repositories.messages import MessageRepository
        from backend.persistence.repositories.remote_requests import (
            RemoteRequestRepository,
        )

        row = await RemoteRequestRepository(session).get(record.request_id)
        assert row is not None and row.status == "PROCESSING"
        assert await MessageRepository(session).get(
            envelope["message_id"]) is None


@pytest.mark.asyncio
async def test_expire_at_exact_boundary(db):
    from backend.requests.errors import InvalidStateTransitionError

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    done = await service.expire(record.request_id, now=record.expires_at)
    assert done.status == "EXPIRED"
    with pytest.raises(InvalidStateTransitionError):
        await service.expire(record.request_id, now=record.expires_at)


@pytest.mark.asyncio
async def test_terminal_requests_cannot_expire(db):
    from backend.requests.errors import InvalidStateTransitionError

    keys = _Keys()
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        sender, recipient = await _pair(session, keys)
    record = await _create(service, sender, recipient)
    cancelled = await service.cancel(record.request_id)
    assert cancelled.status == "CANCELLED"
    with pytest.raises(InvalidStateTransitionError):
        await service.expire(cancelled.request_id, now=EXPIRY)
