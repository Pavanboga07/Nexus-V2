"""Audit M1: near-expiry success must never commit undeliverable COMPLETED.

The response inherits the request's ``expires_at`` and must still pass
the outbox worker's existing expiry validation
(``validate_message(..., now=...)`` rejects ``now >= expires_at``) when
the next dispatch batch runs. A success with remaining lifetime at or
below ``RESPONSE_EXPIRY_MARGIN_S`` therefore takes the existing
mid-flight-expiry failure path (``FAILED``/``REQUEST_EXPIRED``, no
response, no outbox) instead of a terminal ``COMPLETED`` the worker
would permanently reject. All clocks are injected or patched: no test
sleeps for a real timeout.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

from backend.execution import executor as executor_mod
from backend.execution.executor import RESPONSE_EXPIRY_MARGIN_S
from backend.identity.keys import public_key_to_bytes
from backend.identity.verification import verify_message
from backend.persistence.database import session_scope
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
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


async def _set_expiry(db, request_id, expires_at):
    async with session_scope(db.sessions) as session:
        updated = await RemoteRequestRepository(session).update(
            request_id, expires_at=expires_at
        )
        assert updated is not None


async def _message_ids(db, request_id):
    async with session_scope(db.sessions) as session:
        rows = await MessageRepository(session).list_by_request(request_id)
        return {row.message_id for row in rows}


async def _execute_at(executor, request_id, moment):
    """Run with both the claim clock and the finalize clock fixed.

    The patch must stay active across the await: execute() is async,
    so patching around the bare call would unpatch before the body
    (including _finalize's clock read) runs.
    """
    with mock.patch.object(executor_mod, "_utcnow", return_value=moment):
        return await executor.execute(request_id, now=moment)


@pytest.mark.asyncio
async def test_adequate_lifetime_completes_and_queues(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    moment = datetime.now(timezone.utc)
    await _set_expiry(db, request_id, moment + timedelta(hours=1))

    model = AnswerModel(text="fresh")
    outcome = await _execute_at(
        make_executor(db, keys, recipient.agent_id, make_runtime(model)),
        request_id, moment,
    )
    assert outcome.status == "COMPLETED"
    assert outcome.executed is True
    assert outcome.response_message_id is not None
    assert model.calls == 1

    service = RequestService(db.sessions, _ReadKeys())
    stored = await service.get_request(request_id)
    assert stored.status == "COMPLETED"
    assert stored.response_message_id == outcome.response_message_id
    assert len(await response_messages(db, request_id)) == 1
    async with session_scope(db.sessions) as session:
        outbox = await OutboxRepository(session).get(
            outcome.response_message_id)
    assert outbox is not None
    assert outbox.status == "PENDING"


@pytest.mark.asyncio
async def test_remaining_at_margin_fails_without_response(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    moment = datetime.now(timezone.utc)
    await _set_expiry(
        db, request_id,
        moment + timedelta(seconds=RESPONSE_EXPIRY_MARGIN_S),
    )
    before = await _message_ids(db, request_id)

    model = AnswerModel(text="too late to deliver")
    outcome = await _execute_at(
        make_executor(db, keys, recipient.agent_id, make_runtime(model)),
        request_id, moment,
    )
    # The agent ran once (no rerun), but no success was claimed.
    assert outcome.status == "FAILED"
    assert outcome.executed is True
    assert outcome.response_message_id is None
    assert outcome.record.failure_code == "REQUEST_EXPIRED"
    assert model.calls == 1

    service = RequestService(db.sessions, _ReadKeys())
    stored = await service.get_request(request_id)
    assert stored.status == "FAILED"
    assert stored.failure_code == "REQUEST_EXPIRED"
    assert stored.response_message_id is None
    assert await response_messages(db, request_id) == []
    assert await _message_ids(db, request_id) == before

    # Terminal: a repeat replays the failure without re-executing.
    again = await make_executor(
        db, keys, recipient.agent_id, make_runtime(model)).execute(
        request_id, now=moment + timedelta(seconds=1))
    assert again.status == "FAILED"
    assert again.executed is False
    assert model.calls == 1


@pytest.mark.asyncio
async def test_remaining_below_margin_fails_without_response(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    moment = datetime.now(timezone.utc)
    await _set_expiry(
        db, request_id,
        moment + timedelta(seconds=RESPONSE_EXPIRY_MARGIN_S - 1),
    )
    before = await _message_ids(db, request_id)

    model = AnswerModel(text="almost expired")
    outcome = await _execute_at(
        make_executor(db, keys, recipient.agent_id, make_runtime(model)),
        request_id, moment,
    )
    assert outcome.status == "FAILED"
    assert outcome.executed is True
    assert outcome.response_message_id is None
    assert outcome.record.failure_code == "REQUEST_EXPIRED"

    service = RequestService(db.sessions, _ReadKeys())
    stored = await service.get_request(request_id)
    assert stored.status == "FAILED"
    assert stored.failure_code == "REQUEST_EXPIRED"
    assert stored.response_message_id is None
    assert await response_messages(db, request_id) == []
    assert await _message_ids(db, request_id) == before


@pytest.mark.asyncio
async def test_remaining_just_above_margin_completes(db):
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    moment = datetime.now(timezone.utc)
    await _set_expiry(
        db, request_id,
        moment + timedelta(seconds=RESPONSE_EXPIRY_MARGIN_S + 1),
    )

    model = AnswerModel(text="just in time")
    outcome = await _execute_at(
        make_executor(db, keys, recipient.agent_id, make_runtime(model)),
        request_id, moment,
    )
    assert outcome.status == "COMPLETED"
    assert outcome.executed is True
    assert outcome.response_message_id is not None
    assert len(await response_messages(db, request_id)) == 1


@pytest.mark.asyncio
async def test_expiry_during_execution_records_expired_failure(db):
    """Slow runtime: valid at claim time, expired at finalize time."""
    keys = Keys()
    _, recipient, request_id = await seed_approved(db, keys)
    claim_at = datetime.now(timezone.utc)
    await _set_expiry(db, request_id, claim_at + timedelta(seconds=5))
    before = await _message_ids(db, request_id)

    model = AnswerModel(text="stale answer")
    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(model))
    finalize_at = claim_at + timedelta(seconds=60)
    with mock.patch.object(
        executor_mod, "_utcnow", return_value=finalize_at
    ):
        outcome = await executor.execute(request_id, now=claim_at)
    assert outcome.status == "FAILED"
    assert outcome.executed is True
    assert outcome.response_message_id is None
    assert outcome.record.failure_code == "REQUEST_EXPIRED"
    assert model.calls == 1

    service = RequestService(db.sessions, _ReadKeys())
    stored = await service.get_request(request_id)
    assert stored.status == "FAILED"
    assert stored.failure_code == "REQUEST_EXPIRED"
    assert stored.response_message_id is None
    assert await response_messages(db, request_id) == []
    assert await _message_ids(db, request_id) == before


@pytest.mark.asyncio
async def test_success_reaches_worker_validation_unmodified(db):
    """An adequate-lifetime response dispatches with no rewrite/re-sign."""
    import copy

    keys = Keys()
    _, recipient, request_id = await seed_approved(
        db, keys, query="dispatch me before expiry")
    moment = datetime.now(timezone.utc)
    await _set_expiry(db, request_id, moment + timedelta(hours=1))

    outcome = await _execute_at(
        make_executor(
            db, keys, recipient.agent_id,
            make_runtime(AnswerModel(text="deliverable"))),
        request_id, moment,
    )
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
        clock=lambda: moment,
        jitter=lambda: 0.0,
    )
    results = await worker.run_once(batch_size=100)
    ours = [r for r in results
            if r.message_id == outcome.response_message_id]
    assert len(ours) == 1
    assert ours[0].outcome.value == "SENT"

    async with session_scope(db.sessions) as session:
        row = await MessageRepository(session).get(
            outcome.response_message_id)
    assert row is not None
    stored_wire = message_to_dict(message_from_dict(
        {**row.envelope, "signature": row.signature}))
    assert sent["envelope"] == stored_wire
    verify_message(
        message_from_dict(sent["envelope"]),
        Ed25519PublicKey.from_public_bytes(
            public_key_to_bytes(recipient.public_key)),
        now=moment,
    )
    service = RequestService(db.sessions, _ReadKeys())
    assert (await service.get_request(request_id)).status == "COMPLETED"
    async with session_scope(db.sessions) as session:
        outbox = await OutboxRepository(session).get(
            outcome.response_message_id)
    assert outbox is not None
    assert outbox.status == "SENT"
