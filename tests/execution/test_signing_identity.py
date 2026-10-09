"""Audit H1: missing local signing identity fails before any execution.

If ``private_key_for(local_agent_id)`` cannot supply the key, the
executor must fail closed before the ``APPROVED -> PROCESSING`` claim
and before the Agent Runtime (model or MCP tools) runs. The request
stays ``APPROVED`` so it is recoverable once the identity is restored,
and no response or outbox record is created.
"""

import asyncio

import pytest

from backend.execution import ExecutionError
from backend.persistence.database import session_scope
from backend.persistence.repositories.messages import MessageRepository
from backend.requests.service import RequestService

from .conftest import (
    AnswerModel,
    Keys,
    ScriptedModel,
    local_calc_tool,
    make_executor,
    make_runtime,
    response_messages,
    seed_approved,
)


class _NoLocalKey:
    """Identity provider that fails only for the local agent id."""

    def __init__(self, keys, local_agent_id):
        self._keys = keys
        self._local = local_agent_id

    def private_key_for(self, agent_id):
        if agent_id == self._local:
            raise KeyError(agent_id)
        return self._keys.private_key_for(agent_id)


class _ReadKeys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


async def _message_ids(db, request_id):
    async with session_scope(db.sessions) as session:
        rows = await MessageRepository(session).list_by_request(request_id)
        return {row.message_id for row in rows}


@pytest.mark.asyncio
async def test_missing_signing_identity_fails_before_model(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    before = await _message_ids(db, request_id)

    tool_calls_log = []
    model = ScriptedModel()
    from langchain_core.messages import AIMessage

    model.steps = [
        AIMessage(content="", tool_calls=[
            {"name": "calc_add", "args": {"a": 1, "b": 2},
             "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="three"),
    ]
    runtime = make_runtime(model, tools=[local_calc_tool(tool_calls_log)])
    executor = make_executor(
        db, _NoLocalKey(keys, recipient.agent_id),
        recipient.agent_id, runtime,
    )
    with pytest.raises(ExecutionError, match="no signing identity"):
        await executor.execute(request_id)

    # The model never ran, so no MCP tool could have executed either.
    assert model.calls == 0
    assert tool_calls_log == []

    # The claim never committed: still APPROVED, hence recoverable.
    service = RequestService(db.sessions, _ReadKeys())
    stored = await service.get_request(request_id)
    assert stored.status == "APPROVED"
    assert stored.response_message_id is None

    # No response message (hence no outbox row, created atomically with
    # the message in _finalize) was recorded.
    assert await response_messages(db, request_id) == []
    assert await _message_ids(db, request_id) == before

    # Recoverable: restoring the identity lets the same request run.
    retry_model = AnswerModel(text="recovered")
    retry = make_executor(
        db, keys, recipient.agent_id, make_runtime(retry_model))
    outcome = await retry.execute(request_id)
    assert outcome.status == "COMPLETED"
    assert outcome.executed is True
    assert retry_model.calls == 1
    assert len(await response_messages(db, request_id)) == 1


@pytest.mark.asyncio
async def test_missing_signing_identity_concurrent_claims_stay_approved(db):
    """Concurrent claimants serialize on the row lock; all fail closed."""
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    before = await _message_ids(db, request_id)

    model = AnswerModel(text="never")
    executor = make_executor(
        db, _NoLocalKey(keys, recipient.agent_id),
        recipient.agent_id, make_runtime(model),
    )
    outcomes = await asyncio.gather(
        *[executor.execute(request_id) for _ in range(8)],
        return_exceptions=True,
    )
    assert all(isinstance(o, ExecutionError) for o in outcomes), outcomes
    assert model.calls == 0

    service = RequestService(db.sessions, _ReadKeys())
    stored = await service.get_request(request_id)
    assert stored.status == "APPROVED"
    assert stored.response_message_id is None
    assert await response_messages(db, request_id) == []
    assert await _message_ids(db, request_id) == before
