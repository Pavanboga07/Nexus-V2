"""Message repository tests incl. the Component 1/2 round trip."""

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
from backend.identity.verification import verify_message
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.errors import ConflictError
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.serialization import message_from_dict, message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import validate_message

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


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def _two_agents(session):
    identities = []
    for _ in range(2):
        identity = create_identity()
        raw = public_key_to_bytes(identity.public_key)
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        await AgentRepository(session).create(identity.agent_id, encoded)
        identities.append(identity)
    return identities[0], identities[1]


def _signed(sender, recipient_id, request_id, now, message_id=None):
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id or _tag("msg"),
        request_id=request_id,
        sender=sender.agent_id,
        recipient=recipient_id,
        type=MessageType.REMOTE_QUERY,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        payload=RemoteQueryPayload(
            query="What projects are you working on?",
            requested_capabilities=["memory.read"],
        ),
        signature="",
    )
    return sign_message(message, sender.private_key)


async def _stored_request(session, sender, recipient, request_id=None):
    request_id = request_id or _tag("req")
    return await RemoteRequestRepository(session).create(
        request_id=request_id,
        sender_agent_id=sender.agent_id,
        recipient_agent_id=recipient.agent_id,
        query="What projects are you working on?",
        requested_capabilities=["memory.read"],
        status="CREATED",
        created_at=BASE,
        expires_at=BASE + timedelta(hours=1),
    )


async def _persist_signed(session, signed, request_row):
    wire = message_to_dict(signed)
    envelope = {key: value for key, value in wire.items() if key != "signature"}
    return await MessageRepository(session).create(
        message_id=signed.message_id,
        request_id=request_row.request_id,
        sender_agent_id=signed.sender,
        recipient_agent_id=signed.recipient,
        message_type=signed.type.value,
        protocol_version=signed.protocol_version,
        created_at=signed.created_at,
        expires_at=signed.expires_at,
        signature=signed.signature,
        envelope=envelope,
    )


@pytest.mark.asyncio
async def test_create_message(db):  # 24
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request = await _stored_request(session, sender, recipient)
        signed = _signed(sender, recipient.agent_id, request.request_id, BASE)
        row = await _persist_signed(session, signed, request)
        assert row.message_id == signed.message_id


@pytest.mark.asyncio
async def test_retrieve_by_message_id(db):  # 25
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request = await _stored_request(session, sender, recipient)
        signed = _signed(sender, recipient.agent_id, request.request_id, BASE)
        await _persist_signed(session, signed, request)
    async with db.sessions() as session:
        found = await MessageRepository(session).get(signed.message_id)
        assert found is not None
        assert found.sender_agent_id == sender.agent_id
        assert await MessageRepository(session).get("msg_missing") is None


@pytest.mark.asyncio
async def test_retrieve_by_request_id(db):  # 26
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request = await _stored_request(session, sender, recipient)
        first = _signed(sender, recipient.agent_id, request.request_id, BASE)
        second = _signed(sender, recipient.agent_id, request.request_id, BASE)
        await _persist_signed(session, first, request)
        await _persist_signed(session, second, request)
        earliest = await MessageRepository(session).get_by_request_id(
            request.request_id
        )
        assert earliest is not None
        assert earliest.message_id in {first.message_id, second.message_id}
        listed = await MessageRepository(session).list_by_request(
            request.request_id
        )
        assert {row.message_id for row in listed} == {
            first.message_id, second.message_id}


@pytest.mark.asyncio
async def test_duplicate_message_id_rejected(db):  # 27
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request = await _stored_request(session, sender, recipient)
        signed = _signed(sender, recipient.agent_id, request.request_id, BASE)
        await _persist_signed(session, signed, request)
    async with db.sessions() as session:
        with pytest.raises(ConflictError):
            await _persist_signed(session, signed, request)


@pytest.mark.asyncio
async def test_protocol_envelope_round_trip(db):  # 28
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request = await _stored_request(session, sender, recipient)
        signed = _signed(sender, recipient.agent_id, request.request_id, BASE)
        wire = message_to_dict(signed)
        await _persist_signed(session, signed, request)
    async with db.sessions() as session:
        found = await MessageRepository(session).get(signed.message_id)
        assert found is not None
        assert found.envelope == {
            key: value for key, value in wire.items() if key != "signature"
        }


@pytest.mark.asyncio
async def test_signature_preserved_exactly(db):  # 29
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request = await _stored_request(session, sender, recipient)
        signed = _signed(sender, recipient.agent_id, request.request_id, BASE)
        await _persist_signed(session, signed, request)
    async with db.sessions() as session:
        found = await MessageRepository(session).get(signed.message_id)
        assert found is not None
        assert found.signature == signed.signature


@pytest.mark.asyncio
async def test_persisted_message_stays_valid_and_verifiable(db):
    # Full protocol integration: a real signed Component 1 message
    # survives storage with cryptographic validity intact.
    async with session_scope(db.sessions) as session:
        sender, recipient = await _two_agents(session)
        request = await _stored_request(session, sender, recipient)
        signed = _signed(sender, recipient.agent_id, request.request_id, BASE)
        await _persist_signed(session, signed, request)
    async with db.sessions() as session:
        found = await MessageRepository(session).get(signed.message_id)
        assert found is not None
        rebuilt = message_from_dict(
            {**found.envelope, "signature": found.signature}
        )
        assert rebuilt == signed
        assert rebuilt.message_id == signed.message_id
        assert rebuilt.request_id == signed.request_id
        assert rebuilt.sender == signed.sender
        assert rebuilt.recipient == signed.recipient
        assert rebuilt.type is signed.type
        assert rebuilt.payload == signed.payload
        assert rebuilt.created_at == signed.created_at
        assert rebuilt.expires_at == signed.expires_at
        assert rebuilt.signature == signed.signature
        assert validate_message(rebuilt, now=NOW) == []
        result = verify_message(rebuilt, sender.public_key, now=NOW)
        assert result.agent_id == sender.agent_id
