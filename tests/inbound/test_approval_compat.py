"""Approval compatibility: existing approve/reject serve inbound rows."""

import pytest

from backend.identity.identity import create_identity
from backend.inbound import (
    REGISTERED,
    InboundProcessor,
)
from backend.persistence.database import session_scope
from backend.persistence.repositories.approvals import ApprovalRepository
from backend.requests.errors import (
    ApprovalError,
    InvalidStateTransitionError,
    UnknownRequestError,
)
from backend.requests.service import RequestService

from .conftest import (
    NOW,
    seed_recipient_side,
    signed_query,
    unique,
)


class _Keys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


async def _registered(db, query="May I read your calendar?"):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg"),
        query=query,
    )
    outcome = await processor.process(message, now=NOW)
    assert outcome.status == REGISTERED
    return sender, recipient, message


@pytest.mark.asyncio
async def test_recipient_can_approve_inbound(db):
    sender, recipient, message = await _registered(db)
    service = RequestService(db.sessions, _Keys())
    approved = await service.approve(
        message.request_id, recipient.agent_id, reason="looks fine"
    )
    assert approved.status == "APPROVED"
    assert approved.sender_agent_id == sender.agent_id
    assert approved.recipient_agent_id == recipient.agent_id
    async with session_scope(db.sessions) as session:
        row = await ApprovalRepository(session).get_by_request_id(
            message.request_id
        )
    assert row is not None
    assert row.decision == "approved"
    assert row.approving_agent_id == recipient.agent_id
    assert row.reason == "looks fine"


@pytest.mark.asyncio
async def test_recipient_can_reject_inbound(db):
    _, recipient, message = await _registered(db)
    service = RequestService(db.sessions, _Keys())
    rejected = await service.reject(
        message.request_id, recipient.agent_id, reason="too broad"
    )
    assert rejected.status == "REJECTED"
    async with session_scope(db.sessions) as session:
        row = await ApprovalRepository(session).get_by_request_id(
            message.request_id
        )
    assert row is not None
    assert row.decision == "rejected"


@pytest.mark.asyncio
async def test_intruder_and_sender_cannot_decide(db):
    sender, recipient, message = await _registered(db)
    service = RequestService(db.sessions, _Keys())
    with pytest.raises(ApprovalError):
        await service.approve(message.request_id, sender.agent_id)
    with pytest.raises(ApprovalError):
        await service.reject(message.request_id, "agent_intruder")
    stored = await service.get_request(message.request_id)
    assert stored is not None
    assert stored.status == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_second_decision_loses_and_approval_executes_nothing(db):
    _, recipient, message = await _registered(db)
    service = RequestService(db.sessions, _Keys())
    await service.approve(message.request_id, recipient.agent_id)
    with pytest.raises((ApprovalError, InvalidStateTransitionError)):
        await service.reject(message.request_id, recipient.agent_id)
    final = await service.get_request(message.request_id)
    assert final is not None
    assert final.status == "APPROVED"
    # Component 9 stops at the decision: no execution artifacts.
    assert final.response_message_id is None
    assert final.completed_at is None
    assert final.failure_code is None


@pytest.mark.asyncio
async def test_approve_unknown_request_fails(db):
    service = RequestService(db.sessions, _Keys())
    with pytest.raises(UnknownRequestError):
        await service.approve(unique("req"), "agent_nobody")
