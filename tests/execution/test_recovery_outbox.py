"""Atomic persistence, crash recovery, and outbox compatibility."""

import asyncio
import threading
from datetime import datetime, timezone
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from backend.execution import ExecutionConflictError
from backend.identity.verification import verify_message
from backend.identity.keys import public_key_to_bytes
from backend.persistence.database import session_scope
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.protocol.serialization import message_from_dict, message_to_dict
from backend.requests.service import RequestService
from backend.worker.config import WorkerConfig
from backend.worker.outbox_dispatcher import OutboxDispatcher

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


@pytest.mark.asyncio
async def test_finalize_failure_rolls_back_then_stays_put(db):
    from backend.persistence.repositories import messages as messages_mod

    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    model = AnswerModel(text="lost")
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    with mock.patch.object(
        messages_mod.MessageRepository, "create",
        side_effect=RuntimeError("store down"),
    ):
        with pytest.raises(RuntimeError, match="store down"):
            await executor.execute(request_id)
    assert model.calls == 1  # the agent ran; nothing was recorded
    service = RequestService(db.sessions, _ReadKeys())
    assert (await service.get_request(request_id)).status == "PROCESSING"
    assert await _response_messages(db, request_id) == []
    # Ambiguous outcome: never auto-re-executed, even after the fix.
    again = await executor.execute(request_id)
    assert again.status == "IN_PROGRESS"
    assert again.executed is False
    assert model.calls == 1

@pytest.mark.asyncio
async def test_mid_flight_state_change_blocks_response(db):
    keys = Keys()
    sender, recipient, request_id = await seed_approved(db, keys)
    entered = threading.Event()
    proceed = threading.Event()

    class GatedModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "gated-model"

        def _generate(self, messages, stop=None, run_manager=None,
                      **kwargs):
            entered.set()
            assert proceed.wait(timeout=60), "test released the gate"
            return ChatResult(generations=[
                ChatGeneration(message=AIMessage(content="too late"))])

    model = GatedModel()
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    ejected = []

    def runner():
        try:
            ejected.append(asyncio.run(executor.execute(request_id)))
        except Exception as exc:  # noqa: BLE001 - asserted below
            ejected.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        service = RequestService(db.sessions, keys)
        await service.fail(request_id, "OPERATOR_STOP", "operator intervened")
        proceed.set()
        await asyncio.to_thread(thread.join, 60)
    finally:
        proceed.set()
        if thread.is_alive():
            thread.join(10)
    assert len(ejected) == 1
    assert isinstance(ejected[0], ExecutionConflictError), ejected[0]
    service = RequestService(db.sessions, _ReadKeys())
    stored = await service.get_request(request_id)
    assert stored.status == "FAILED"
    assert stored.failure_code == "OPERATOR_STOP"
    assert stored.response_message_id is None
    assert await _response_messages(db, request_id) == []


@pytest.mark.asyncio
async def test_response_dispatches_through_existing_worker(db):
    import copy

    keys = Keys()
    sender, recipient, request_id = await seed_approved(
        db, keys, query="dispatch me")
    model = AnswerModel(text="dispatched answer")
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    outcome = await executor.execute(request_id)
    assert outcome.status == "COMPLETED"

    sent = {}

    class FakeClient:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.state = "CONNECTED"

        async def ensure_connected(self):
            return None

        async def send_message(self, envelope, timeout=None):
            from backend.gateway_client.models import AckResult as Ack

            sent["envelope"] = copy.deepcopy(envelope)
            return Ack(envelope["message_id"], "DELIVERED", "ok",
                       envelope.get("request_id", ""))

        async def close(self):
            self.state = "CLOSED"

    worker = OutboxDispatcher(
        sessions=db.sessions,
        identities=keys,
        gateway_url="ws://test",
        service=RequestService(db.sessions, keys),
        config=WorkerConfig(),
        client_factory=lambda sender_id: FakeClient(sender_id),
        clock=lambda: datetime.now(timezone.utc),
        jitter=lambda: 0.0,
    )
    results = await worker.run_once(batch_size=100)
    ours = [r for r in results
            if r.message_id == outcome.response_message_id]
    assert len(ours) == 1
    assert ours[0].outcome.value == "SENT"
    # The worker sent the stored bytes: no rewriting, no re-signing.
    async with session_scope(db.sessions) as session:
        row = await MessageRepository(session).get(
            outcome.response_message_id)
    stored_wire = message_to_dict(message_from_dict(
        {**row.envelope, "signature": row.signature}))
    assert sent["envelope"] == stored_wire
    verify_message(
        message_from_dict(sent["envelope"]),
        Ed25519PublicKey.from_public_bytes(
            public_key_to_bytes(recipient.public_key)),
    )
    # The request row is untouched by dispatch bookkeeping.
    service = RequestService(db.sessions, _ReadKeys())
    assert (await service.get_request(request_id)).status == "COMPLETED"
    async with session_scope(db.sessions) as session:
        outbox = await OutboxRepository(session).get(
            outcome.response_message_id)
    assert outbox is not None
    assert outbox.status == "SENT"


async def _response_messages(db, request_id):
    return await response_messages(db, request_id)


@pytest.mark.asyncio
async def test_error_response_dispatches_without_live_gateway(db):
    keys = Keys()
    _, _, request_id = await seed_approved(db, keys)
    from .conftest import ScriptedModel
    from backend.agent.errors import ModelInvocationError

    model = ScriptedModel()
    model.steps = [ModelInvocationError("model invocation failed")]
    service = RequestService(db.sessions, _ReadKeys())
    record = await service.get_request(request_id)
    executor = make_executor(
        db, keys, record.recipient_agent_id, make_runtime(model))
    outcome = await executor.execute(request_id)
    assert outcome.status == "FAILED"
    async with session_scope(db.sessions) as session:
        outbox = await OutboxRepository(session).get(
            outcome.response_message_id)
    assert outbox is not None
    assert outbox.status == "PENDING"
