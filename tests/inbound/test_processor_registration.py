"""Successful inbound registration tests (real PostgreSQL)."""

import pytest

from backend.identity.identity import create_identity
from backend.inbound import (
    DUPLICATE,
    REGISTERED,
    InboundProcessor,
)
from backend.persistence.database import session_scope
from backend.persistence.repositories.approvals import ApprovalRepository
from backend.persistence.repositories.audit_events import AuditEventRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.requests.service import RequestService

from .conftest import (
    EXPIRY,
    NOW,
    seed_recipient_side,
    signed_query,
    unique,
)


class _Keys:
    def __init__(self):
        self._keys = {}

    def private_key_for(self, agent_id):
        return self._keys[agent_id]


@pytest.mark.asyncio
async def test_valid_query_registers_for_approval(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    outcome = await processor.process(message, now=NOW)
    assert outcome.status == REGISTERED
    assert outcome.request_id == message.request_id
    assert outcome.message_id == message.message_id
    record = outcome.record
    assert record.sender_agent_id == sender.agent_id
    assert record.recipient_agent_id == recipient.agent_id
    assert record.status == "WAITING_APPROVAL"
    assert record.query == "May I read your calendar?"
    assert record.requested_capabilities == ("calendar.read",)
    assert record.created_at == message.created_at
    assert record.expires_at == EXPIRY
    # The signed message is stored verbatim (signature split out).
    async with session_scope(db.sessions) as session:
        stored = await MessageRepository(session).get(message.message_id)
        assert stored is not None
        assert stored.request_id == message.request_id
        assert stored.sender_agent_id == sender.agent_id
        assert stored.recipient_agent_id == recipient.agent_id
        assert stored.message_type == "REMOTE_QUERY"
        assert stored.signature == message.signature
        assert stored.signature not in str(stored.envelope)
        assert stored.envelope["payload"]["query"] == (
            "May I read your calendar?"
        )
        # No approval decision exists yet: approval comes later.
        approval = await ApprovalRepository(session).get_by_request_id(
            message.request_id
        )
        assert approval is None
        events = await AuditEventRepository(session).list_by_request(
            message.request_id
        )
        assert [event.event_type for event in events] == [
            "inbound_request_registered"
        ]
        assert events[0].actor_agent_id == sender.agent_id
        assert events[0].message_id == message.message_id
    # Immediately visible through the existing request-service read API.
    service = RequestService(db.sessions, _Keys())
    seen = await service.get_request(message.request_id)
    assert seen is not None
    assert seen.status == "WAITING_APPROVAL"
    assert seen.message_id == message.message_id


@pytest.mark.asyncio
async def test_wrong_message_type_rejected_without_writes(db):
    from backend.protocol.models import ApprovalRequestPayload
    import dataclasses

    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    other = dataclasses.replace(
        message,
        type=message.type.__class__.APPROVAL_REQUEST,
        payload=ApprovalRequestPayload(
            request_ref=message.request_id,
            query="May I read your calendar?",
            requested_capabilities=["calendar.read"],
        ),
    )
    from backend.inbound import InboundUnsupportedType

    with pytest.raises(InboundUnsupportedType):
        await processor.process(other, now=NOW)
    service = RequestService(db.sessions, _Keys())
    assert await service.get_request(message.request_id) is None


@pytest.mark.asyncio
async def test_replay_registry_never_poisons_before_commit(db):
    from backend.identity.replay import ReplayRegistry

    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    registry = ReplayRegistry()
    processor = InboundProcessor(
        db.sessions, recipient.agent_id, replay_registry=registry
    )
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    # A naive pre-commit mark must not turn a legitimate first delivery
    # into a replay rejection.
    registry.record(message.message_id, message.expires_at)
    outcome = await processor.process(message, now=NOW)
    assert outcome.status == REGISTERED
    assert registry.has_seen(message.message_id, NOW)


@pytest.mark.asyncio
async def test_replay_survives_processor_restart(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    first = InboundProcessor(db.sessions, recipient.agent_id)
    assert (await first.process(message, now=NOW)).status == REGISTERED
    # A fresh processor (new process equivalent) still recognises it:
    # durability comes from the messages table, not memory.
    second = InboundProcessor(db.sessions, recipient.agent_id)
    outcome = await second.process(message, now=NOW)
    assert outcome.status == DUPLICATE
    assert outcome.request_id == message.request_id
