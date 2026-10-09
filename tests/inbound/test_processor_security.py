"""Inbound security validation tests (real PostgreSQL).

Every rejected message must leave no request, message, approval, or
audit rows behind.
"""

import dataclasses
from datetime import timedelta

import pytest

from backend.identity.identity import create_identity
from backend.inbound import (
    InboundExpired,
    InboundInactiveSender,
    InboundInvalidPayload,
    InboundInvalidSignature,
    InboundPeerError,
    InboundProcessor,
    InboundSenderKeyError,
    InboundUnknownSender,
    InboundWrongRecipient,
)
from backend.persistence.database import session_scope
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.audit_events import AuditEventRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.requests.service import (
    MAX_CAPABILITIES,
    MAX_QUERY_LENGTH,
    RequestService,
)

from .conftest import (
    EXPIRY,
    NOW,
    _pub,
    seed_recipient_side,
    signed_query,
    unique,
)


class _Keys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


async def assert_no_trace(db, request_id, message_id):
    service = RequestService(db.sessions, _Keys())
    assert await service.get_request(request_id) is None
    async with session_scope(db.sessions) as session:
        assert await MessageRepository(session).get(message_id) is None
        assert (
            await AuditEventRepository(session).list_by_request(request_id)
            == []
        )


@pytest.mark.asyncio
async def test_tampered_signature_rejected(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    tampered = dataclasses.replace(
        message,
        payload=dataclasses.replace(message.payload, query="PWNED"),
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundInvalidSignature):
        await processor.process(tampered, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_unknown_sender_rejected(db):
    sender, recipient = create_identity(), create_identity()
    # Recipient row only; the sender was never recorded here.
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient)
        )
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundUnknownSender):
        await processor.process(message, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_inactive_sender_rejected(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).deactivate(sender.agent_id)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundInactiveSender):
        await processor.process(message, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_stored_key_mismatch_rejected(db):
    sender, recipient, impostor = (
        create_identity(),
        create_identity(),
        create_identity(),
    )
    await seed_recipient_side(db, sender, recipient)
    # Poison the stored sender key: it no longer derives the sender id.
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).update(
            sender.agent_id, public_key=_pub(impostor)
        )
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundSenderKeyError):
        await processor.process(message, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_undecodable_stored_key_rejected(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).update(
            sender.agent_id, public_key="!!!not-base64!!!"
        )
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundSenderKeyError):
        await processor.process(message, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_missing_peer_rejected(db):
    sender, recipient = create_identity(), create_identity()
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient)
        )
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundPeerError):
        await processor.process(message, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_pending_peer_rejected(db):
    sender, recipient = create_identity(), create_identity()
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient)
        )
        await PeerRepository(session).create(
            recipient.agent_id, sender.agent_id, status="pending"
        )
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundPeerError):
        await processor.process(message, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_wrong_recipient_rejected(db):
    sender, recipient, stranger = (
        create_identity(),
        create_identity(),
        create_identity(),
    )
    await seed_recipient_side(db, sender, recipient)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    processor = InboundProcessor(db.sessions, stranger.agent_id)
    with pytest.raises(InboundWrongRecipient):
        await processor.process(message, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_expired_message_rejected(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundExpired):
        await processor.process(message, now=EXPIRY + timedelta(seconds=1))
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_oversized_query_rejected_before_crypto(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg"),
        query="x" * (MAX_QUERY_LENGTH + 1),
    )
    # A garbage signature must not change the verdict: payload policy
    # is enforced before any cryptographic work.
    unsigned = dataclasses.replace(message, signature="garbage")
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundInvalidPayload):
        await processor.process(unsigned, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_too_many_capabilities_rejected(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg"),
        capabilities=[f"cap.{index}" for index in range(MAX_CAPABILITIES + 1)],
    )
    unsigned = dataclasses.replace(message, signature="garbage")
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundInvalidPayload):
        await processor.process(unsigned, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)


@pytest.mark.asyncio
async def test_malformed_capability_rejected(db):
    from backend.protocol.models import RemoteQueryPayload

    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    forged = dataclasses.replace(
        message,
        payload=RemoteQueryPayload(
            query="May I read your calendar?",
            requested_capabilities=["not a capability!!"],
        ),
        signature="garbage",
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    with pytest.raises(InboundInvalidPayload):
        await processor.process(forged, now=NOW)
    await assert_no_trace(db, message.request_id, message.message_id)
