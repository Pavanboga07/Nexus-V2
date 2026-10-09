"""Execution authorization tests: only approved inbound work runs."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.execution import (
    ApprovalMissingError,
    NotInboundRequestError,
    RemoteExecutor,
    RequestExpiredError,
    RequestNotApprovedError,
)
from backend.identity.identity import create_identity
from backend.persistence.database import session_scope
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.requests.errors import (
    PeerError,
    UnknownRequestError,
)
from backend.requests.service import RequestService

from .conftest import (
    AnswerModel,
    Keys,
    _pub,
    live_times,
    make_executor,
    make_runtime,
    seed_approved,
    signed_query,
    unique,
)


@pytest.mark.asyncio
async def test_approved_inbound_executes(db):
    keys = Keys()
    sender, recipient, request_id = await seed_approved(db, keys)
    model = AnswerModel(text="four")
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    outcome = await executor.execute(request_id)
    assert outcome.status == "COMPLETED"
    assert outcome.executed is True
    assert outcome.request_id == request_id
    assert outcome.response_message_id is not None
    assert outcome.record.status == "COMPLETED"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_unapproved_request_rejected_without_model(db):
    keys = Keys()
    sender, recipient = create_identity(), create_identity()
    keys.add(sender)
    keys.add(recipient)
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            recipient.agent_id, sender.agent_id, status="connected")
    from backend.inbound import InboundProcessor

    created, expires = live_times()
    message = signed_query(
        sender, recipient, "Will this run?", unique("req"), unique("msg"),
        created, expires)
    outcome = await InboundProcessor(
        db.sessions, recipient.agent_id).process(message)
    assert outcome.status == "REGISTERED"
    model = AnswerModel()
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    with pytest.raises(RequestNotApprovedError):
        await executor.execute(message.request_id)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_outbound_approved_row_cannot_execute(db):
    keys = Keys()
    local = keys.add(create_identity())
    remote = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(local.agent_id, _pub(local))
        await AgentRepository(session).create(remote.agent_id, _pub(remote))
        await PeerRepository(session).create(
            local.agent_id, remote.agent_id, status="connected")
    service = RequestService(db.sessions, keys)
    created, expires = live_times()
    record = await service.create_remote_request(
        local.agent_id, remote.agent_id, "Local query?", ["calendar.read"],
        expires, now=created)
    record = await service.mark_sent(record.request_id)
    record = await service.mark_delivered(record.request_id)
    record = await service.mark_waiting_approval(record.request_id)
    approved = await service.approve(record.request_id, remote.agent_id)
    assert approved.status == "APPROVED"
    model = AnswerModel()
    executor = make_executor(
        db, keys, local.agent_id, make_runtime(model))
    with pytest.raises(NotInboundRequestError):
        await executor.execute(record.request_id)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_wrong_recipient_executor_rejected(db):
    keys = Keys()
    _, _, request_id = await seed_approved(db, keys)
    other = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(other.agent_id, _pub(other))
    model = AnswerModel()
    executor = make_executor(db, keys, other.agent_id, make_runtime(model))
    with pytest.raises(NotInboundRequestError):
        await executor.execute(request_id)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_expired_approved_marks_expired_without_model(db):
    keys = Keys()
    sender, recipient, request_id = await seed_approved(db, keys)
    service = RequestService(db.sessions, keys)
    stored = await service.get_request(request_id)
    model = AnswerModel()
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    with pytest.raises(RequestExpiredError):
        await executor.execute(
            request_id, now=stored.expires_at + timedelta(seconds=1))
    assert model.calls == 0
    assert (await service.get_request(request_id)).status == "EXPIRED"


@pytest.mark.asyncio
async def test_status_without_approval_row_rejected(db):
    keys = Keys()
    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))
        await PeerRepository(session).create(
            recipient.agent_id, sender.agent_id, status="connected")
    created, expires = live_times()
    async with session_scope(db.sessions) as session:
        await RemoteRequestRepository(session).create(
            request_id=unique("req"),
            sender_agent_id=sender.agent_id,
            recipient_agent_id=recipient.agent_id,
            query="Sneaky?",
            requested_capabilities=["calendar.read"],
            status="APPROVED",
            created_at=created,
            expires_at=expires,
        )
    model = AnswerModel()
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    # The row below was created by the fixture above; fetch its id.
    async with session_scope(db.sessions) as session:
        rows = await RemoteRequestRepository(session).list_by_recipient(
            recipient.agent_id, limit=1000)
    target = [row for row in rows if row.query == "Sneaky?"][0]
    with pytest.raises(ApprovalMissingError):
        await executor.execute(target.request_id)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_revoked_peer_blocks_execution(db):
    keys = Keys()
    sender, recipient, request_id = await seed_approved(db, keys)
    service = RequestService(db.sessions, keys)
    async with session_scope(db.sessions) as session:
        await PeerRepository(session).revoke(
            recipient.agent_id, sender.agent_id,
            datetime.now(timezone.utc),
        )
    model = AnswerModel()
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    with pytest.raises(PeerError):
        await executor.execute(request_id)
    assert model.calls == 0
    assert (await service.get_request(request_id)).status == "APPROVED"


@pytest.mark.asyncio
async def test_unknown_request_rejected(db):
    keys = Keys()
    recipient = keys.add(create_identity())
    model = AnswerModel()
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    with pytest.raises(UnknownRequestError):
        await executor.execute(unique("req"))
    assert model.calls == 0
