"""Dispatcher response-handler seam: routing, armor, retention (real PG).

A registered handler receives REMOTE_RESPONSE messages while queries
and other types keep their existing paths. Handler bugs never kill
the consumer loop; transient failures retain the message for retry.
"""

import asyncio

import pytest

from backend.identity.identity import create_identity
from backend.inbound import (
    HANDLED,
    REJECTED,
    UNHANDLED,
    HandlerAlreadyRegistered,
    InboundDispatcher,
    InboundProcessor,
)
from backend.protocol.models import RemoteResponsePayload
from backend.protocol.types import MessageType
from backend.requests.errors import RetryableInfrastructureError
from backend.responses.models import DUPLICATE, REGISTERED

from .conftest import seed_recipient_side, unique
from .conftest import live_query as signed_query
from .test_dispatcher import _response_message


def _outcome(status, detail=""):
    from backend.responses.models import ResponseOutcome

    return ResponseOutcome(
        status=status, request_id=unique("req"),
        response_message_id=unique("msg"), detail=detail,
    )


@pytest.mark.asyncio
async def test_response_reaches_registered_handler(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, recipient.agent_id))
    seen = []

    async def handler(message):
        seen.append(message.message_id)
        return _outcome(REGISTERED, detail="COMPLETED")

    dispatcher.set_response_handler(handler)
    response = _response_message(recipient, sender)
    result = await dispatcher.dispatch_one(response)
    assert result.status == HANDLED
    assert result.detail == "COMPLETED"
    assert seen == [response.message_id]
    assert dispatcher.unhandled.empty()


@pytest.mark.asyncio
async def test_double_registration_rejected(db):
    sender, recipient = create_identity(), create_identity()
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, recipient.agent_id))

    async def first(message):
        return _outcome(REGISTERED)

    async def second(message):
        return _outcome(REGISTERED)

    dispatcher.set_response_handler(first)
    with pytest.raises(HandlerAlreadyRegistered):
        dispatcher.set_response_handler(second)
    # Clearing a foreign handler never steals the registration.
    dispatcher.clear_response_handler(second)
    response = _response_message(recipient, sender)
    calls = []

    async def spy(message):
        calls.append(message.message_id)
        return _outcome(REGISTERED)

    with pytest.raises(HandlerAlreadyRegistered):
        dispatcher.set_response_handler(spy)
    dispatcher.clear_response_handler(first)
    dispatcher.set_response_handler(spy)
    await dispatcher.dispatch_one(response)
    assert calls == [response.message_id]


@pytest.mark.asyncio
async def test_handler_exception_keeps_dispatcher_alive(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, recipient.agent_id))

    async def buggy(message):
        raise RuntimeError("handler bug")

    dispatcher.set_response_handler(buggy)
    source: asyncio.Queue = asyncio.Queue()
    task = dispatcher.start(source.get)
    try:
        await source.put(_response_message(recipient, sender))
        await asyncio.sleep(0.5)
        assert dispatcher.running
        # Queries still flow after the handler blew up.
        await source.put(signed_query(
            sender, recipient, request_id=unique("req"),
            message_id=unique("msg"),
        ))
        for _ in range(100):
            if not dispatcher.unhandled.empty():
                break
            await asyncio.sleep(0.05)
    finally:
        await dispatcher.stop()
    assert not dispatcher.running


@pytest.mark.asyncio
async def test_handler_exception_reports_rejected(db):
    sender, recipient = create_identity(), create_identity()
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, recipient.agent_id))

    async def buggy(message):
        raise RuntimeError("handler bug")

    dispatcher.set_response_handler(buggy)
    result = await dispatcher.dispatch_one(
        _response_message(recipient, sender))
    assert result.status == REJECTED
    assert result.detail == "RuntimeError"
    assert dispatcher.unhandled.empty()


@pytest.mark.asyncio
async def test_transient_failure_retains_for_retry(db):
    sender, recipient = create_identity(), create_identity()
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, recipient.agent_id))
    attempts = []

    async def flaky(message):
        attempts.append(message.message_id)
        if len(attempts) == 1:
            raise RetryableInfrastructureError("db blip")
        return _outcome(REGISTERED, detail="COMPLETED")

    dispatcher.set_response_handler(flaky)
    response = _response_message(recipient, sender)
    result = await dispatcher.dispatch_one(response)
    assert result.status == REJECTED
    assert result.detail == "retained-for-retry"
    retained = await asyncio.wait_for(
        dispatcher.next_unhandled(), timeout=5)
    assert retained.message_id == response.message_id
    # The retry then succeeds through the normal dispatch path.
    again = await dispatcher.dispatch_one(retained)
    assert again.status == HANDLED
    assert attempts == [response.message_id] * 2


@pytest.mark.asyncio
async def test_other_types_kept_with_handler_registered(db):
    import dataclasses

    from backend.protocol.models import ApprovalRequestPayload

    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, recipient.agent_id))

    async def handler(message):
        return _outcome(DUPLICATE, detail="already-processed")

    dispatcher.set_response_handler(handler)
    query = signed_query(
        sender, recipient, request_id=unique("req"),
        message_id=unique("msg"),
    )
    assert (await dispatcher.dispatch_one(query)).status == HANDLED
    other = dataclasses.replace(
        query,
        type=MessageType.APPROVAL_REQUEST,
        payload=ApprovalRequestPayload(
            request_ref=query.request_id,
            query="May I read your calendar?",
            requested_capabilities=["calendar.read"],
        ),
    )
    assert (await dispatcher.dispatch_one(other)).status == UNHANDLED
    response = _response_message(recipient, sender)
    assert (await dispatcher.dispatch_one(response)).status == HANDLED
    kept = await asyncio.wait_for(dispatcher.next_unhandled(), timeout=5)
    assert kept.message_id == other.message_id


@pytest.mark.asyncio
async def test_clear_handler_restores_retention(db):
    sender, recipient = create_identity(), create_identity()
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, recipient.agent_id))

    async def handler(message):
        return _outcome(REGISTERED)

    dispatcher.set_response_handler(handler)
    dispatcher.clear_response_handler(handler)
    response = _response_message(recipient, sender)
    assert (await dispatcher.dispatch_one(response)).status == UNHANDLED
    assert (await asyncio.wait_for(
        dispatcher.next_unhandled(), timeout=5)).message_id == (
        response.message_id)


@pytest.mark.asyncio
async def test_retention_backpressure_with_handler(db):
    sender, recipient = create_identity(), create_identity()
    dispatcher = InboundDispatcher(
        InboundProcessor(db.sessions, recipient.agent_id),
        unhandled_queue_size=1,
    )

    async def handler(message):
        return _outcome(REGISTERED)

    dispatcher.set_response_handler(handler)
    import dataclasses

    from backend.protocol.models import ApprovalRequestPayload

    def _other():
        query = signed_query(
            sender, recipient, request_id=unique("req"),
            message_id=unique("msg"),
        )
        return dataclasses.replace(
            query,
            type=MessageType.APPROVAL_REQUEST,
            payload=ApprovalRequestPayload(
                request_ref=query.request_id,
                query="May I read your calendar?",
                requested_capabilities=["calendar.read"],
            ),
        )

    assert (await dispatcher.dispatch_one(_other())).status == UNHANDLED
    # The queue is full: the next retention blocks until drained.
    pending = asyncio.ensure_future(dispatcher.dispatch_one(_other()))
    await asyncio.sleep(0.3)
    assert not pending.done()
    drained = await asyncio.wait_for(dispatcher.next_unhandled(), timeout=5)
    assert drained is not None
    assert (await asyncio.wait_for(pending, timeout=5)).status == UNHANDLED
