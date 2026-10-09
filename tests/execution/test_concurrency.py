"""Concurrency and idempotency: one claim, one run, stable dedup."""

import asyncio

import pytest

from backend.persistence.database import session_scope
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.requests.service import RequestService

from .conftest import (
    AnswerModel,
    Keys,
    make_executor,
    make_runtime,
    response_messages,
    seed_approved,
)


class _ReadKeys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


async def _outbox_rows(db, message_id):
    async with session_scope(db.sessions) as session:
        row = await OutboxRepository(session).get(message_id)
        return row


@pytest.mark.asyncio
async def test_concurrent_calls_execute_exactly_once(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    model = AnswerModel(text="once")
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    outcomes = await asyncio.gather(
        *[executor.execute(request_id) for _ in range(8)]
    )
    ran = [o for o in outcomes if o.executed]
    assert len(ran) == 1
    assert model.calls == 1
    assert {o.status for o in outcomes} <= {"COMPLETED", "IN_PROGRESS"}
    assert {o.request_id for o in outcomes} == {request_id}
    winner = ran[0]
    assert winner.status == "COMPLETED"
    messages = await response_messages(db, request_id)
    assert len(messages) == 1
    assert messages[0].message_id == winner.response_message_id
    row = await _outbox_rows(db, winner.response_message_id)
    assert row is not None
    assert row.status == "PENDING"
    service = RequestService(db.sessions, _ReadKeys())
    assert (await service.get_request(request_id)).status == "COMPLETED"


@pytest.mark.asyncio
async def test_repeat_after_completion_does_not_rerun(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    model = AnswerModel(text="stable")
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    first = await executor.execute(request_id)
    assert first.status == "COMPLETED" and first.executed is True
    second = await executor.execute(request_id)
    assert second.status == "COMPLETED"
    assert second.executed is False
    assert second.response_message_id == first.response_message_id
    assert model.calls == 1
    assert len(await response_messages(db, request_id)) == 1


@pytest.mark.asyncio
async def test_two_requests_claim_independently(db):
    keys = Keys()
    model = AnswerModel(text="both")
    service = RequestService(db.sessions, _ReadKeys())
    executors = []
    ids = []
    for _ in range(2):
        _, recipient, request_id = await seed_approved(db, keys)
        ids.append(request_id)
        executors.append(make_executor(
            db, keys, recipient.agent_id, make_runtime(model)))
    first, second = await asyncio.gather(
        executors[0].execute(ids[0]), executors[1].execute(ids[1]))
    assert first.status == "COMPLETED" and first.executed is True
    assert second.status == "COMPLETED" and second.executed is True
    assert first.request_id == ids[0]
    assert second.request_id == ids[1]
    assert first.response_message_id != second.response_message_id
    assert model.calls == 2


@pytest.mark.asyncio
async def test_stuck_processing_never_auto_reexecutes(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    service = RequestService(db.sessions, keys)
    moved = await service.mark_processing(request_id)
    assert moved.status == "PROCESSING"
    model = AnswerModel()
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    outcome = await executor.execute(request_id)
    assert outcome.status == "IN_PROGRESS"
    assert outcome.executed is False
    assert model.calls == 0
    again = await executor.execute(request_id)
    assert again.status == "IN_PROGRESS"
    assert again.executed is False
    assert model.calls == 0


@pytest.mark.asyncio
async def test_recreated_executor_respects_claims(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    model = AnswerModel(text="durable")
    first = make_executor(db, keys, recipient.agent_id, make_runtime(model))
    outcome = await first.execute(request_id)
    assert outcome.status == "COMPLETED"
    second = make_executor(db, keys, recipient.agent_id, make_runtime(model))
    again = await second.execute(request_id)
    assert again.status == "COMPLETED"
    assert again.executed is False
    assert again.response_message_id == outcome.response_message_id
    assert model.calls == 1
