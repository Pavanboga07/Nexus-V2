"""Inbound idempotency and atomicity tests (real PostgreSQL)."""

import asyncio
from unittest import mock

import pytest
from sqlalchemy.exc import OperationalError

from backend.identity.identity import create_identity
from backend.inbound import (
    DUPLICATE,
    REGISTERED,
    InboundConflictingDuplicate,
    InboundInfrastructureError,
    InboundProcessor,
)
from backend.persistence.database import session_scope
from backend.persistence.repositories.approvals import ApprovalRepository
from backend.persistence.repositories.audit_events import AuditEventRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
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


async def counts(db, request_id):
    async with session_scope(db.sessions) as session:
        requests = await RemoteRequestRepository(session).list_by_status(
            "WAITING_APPROVAL", limit=1000
        )
        mine = [row for row in requests if row.request_id == request_id]
        messages = await MessageRepository(session).list_by_request(
            request_id
        )
        approval = await ApprovalRepository(session).get_by_request_id(
            request_id
        )
        events = await AuditEventRepository(session).list_by_request(
            request_id
        )
    return mine, messages, approval, events


@pytest.mark.asyncio
async def test_exact_duplicate_returns_idempotent_outcome(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    first = await processor.process(message, now=NOW)
    assert first.status == REGISTERED
    second = await processor.process(message, now=NOW)
    assert second.status == DUPLICATE
    assert second.request_id == message.request_id
    assert second.record.status == "WAITING_APPROVAL"
    mine, messages, approval, events = await counts(db, message.request_id)
    assert len(mine) == 1
    assert len(messages) == 1
    assert approval is None
    assert [event.event_type for event in events] == [
        "inbound_request_registered"
    ]


@pytest.mark.asyncio
async def test_conflicting_request_content_rejected(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    request_id = unique("req")
    original = signed_query(
        sender, recipient, request_id=request_id, message_id=unique("msg"),
        query="May I read your calendar?",
    )
    assert (await processor.process(original, now=NOW)).status == REGISTERED
    rival = signed_query(
        sender, recipient, request_id=request_id, message_id=unique("msg"),
        query="Delete everything instead",
    )
    with pytest.raises(InboundConflictingDuplicate):
        await processor.process(rival, now=NOW)
    # The stored request is untouched and still approvable.
    service = RequestService(db.sessions, _Keys())
    stored = await service.get_request(request_id)
    assert stored is not None
    assert stored.query == "May I read your calendar?"
    assert stored.status == "WAITING_APPROVAL"
    approved = await service.approve(request_id, recipient.agent_id)
    assert approved.status == "APPROVED"
    # A rejected conflict leaves no new rows: only the original
    # registration event exists.
    async with session_scope(db.sessions) as session:
        events = await AuditEventRepository(session).list_by_request(
            request_id
        )
    assert [event.event_type for event in events] == [
        "inbound_request_registered"
    ]


@pytest.mark.asyncio
async def test_message_id_reuse_across_requests_rejected(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    message_id = unique("msg")
    first = signed_query(
        sender, recipient, request_id=unique("req"), message_id=message_id
    )
    assert (await processor.process(first, now=NOW)).status == REGISTERED
    second = signed_query(
        sender, recipient, request_id=unique("req"), message_id=message_id
    )
    with pytest.raises(InboundConflictingDuplicate):
        await processor.process(second, now=NOW)
    mine, messages, _, _ = await counts(db, first.request_id)
    assert len(mine) == 1
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicates_register_exactly_once(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    outcomes = await asyncio.gather(
        *[processor.process(message, now=NOW) for _ in range(8)]
    )
    statuses = sorted(outcome.status for outcome in outcomes)
    assert statuses == [DUPLICATE] * 7 + [REGISTERED]
    mine, messages, approval, _ = await counts(db, message.request_id)
    assert len(mine) == 1
    assert len(messages) == 1
    assert approval is None


@pytest.mark.asyncio
async def test_persistence_failure_rolls_back_everything(db):
    from backend.persistence.repositories import audit_events

    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    with mock.patch.object(
        audit_events.AuditEventRepository,
        "create",
        side_effect=RuntimeError("disk is gone"),
    ):
        with pytest.raises(RuntimeError, match="disk is gone"):
            await processor.process(message, now=NOW)
    service = RequestService(db.sessions, _Keys())
    assert await service.get_request(message.request_id) is None
    async with session_scope(db.sessions) as session:
        assert await MessageRepository(session).get(message.message_id) is None
    # And the same message registers cleanly once the fault clears.
    outcome = await processor.process(message, now=NOW)
    assert outcome.status == REGISTERED


@pytest.mark.asyncio
async def test_transient_db_failure_is_retryable_then_recovers(db):
    from backend.persistence.repositories import remote_requests

    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    real_get = remote_requests.RemoteRequestRepository.get
    calls = 0

    async def flaky_get(self, request_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("select 1", {}, Exception("conn lost"))
        return await real_get(self, request_id)

    with mock.patch.object(
        remote_requests.RemoteRequestRepository, "get", flaky_get
    ):
        with pytest.raises(InboundInfrastructureError):
            await processor.process(message, now=NOW)
        # The failed attempt stored nothing, so the retry registers.
        outcome = await processor.process(message, now=NOW)
        assert outcome.status == REGISTERED
    service = RequestService(db.sessions, _Keys())
    stored = await service.get_request(message.request_id)
    assert stored is not None
    assert stored.status == "WAITING_APPROVAL"
