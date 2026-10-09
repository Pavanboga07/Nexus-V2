"""Inbound dispatcher tests: routing, retention, lifecycle (real PG)."""

import asyncio

import pytest

from backend.identity.identity import create_identity
from backend.inbound import (
    HANDLED,
    REJECTED,
    UNHANDLED,
    DispatcherAlreadyRunning,
    InboundDispatcher,
    InboundProcessor,
)
from backend.protocol.models import RemoteResponsePayload
from backend.protocol.types import MessageType
from backend.requests.service import RequestService

from .conftest import (
    EXPIRY,
    NOW,
    seed_recipient_side,
    unique,
)
from .conftest import live_query as signed_query


class _Keys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


def _response_message(recipient, sender):
    from backend.identity.signing import sign_message
    from backend.protocol.models import NexusMessage
    from backend.protocol.types import PROTOCOL_VERSION

    unsigned = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=unique("msg"),
        request_id=unique("req"),
        sender=sender.agent_id,
        recipient=recipient.agent_id,
        type=MessageType.REMOTE_RESPONSE,
        created_at=NOW,
        expires_at=EXPIRY,
        payload=RemoteResponsePayload(status="ok", answer="done"),
        signature="",
    )
    # Retention routing never validates liveness or signatures; the
    # future receiver will.
    return unsigned


@pytest.mark.asyncio
async def test_query_reaches_processor(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    dispatcher = InboundDispatcher(processor)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    result = await dispatcher.dispatch_one(message)
    assert result.status == HANDLED
    assert result.detail == "REGISTERED"
    assert result.request_id == message.request_id
    service = RequestService(db.sessions, _Keys())
    assert (await service.get_request(message.request_id)).status == (
        "WAITING_APPROVAL"
    )


@pytest.mark.asyncio
async def test_response_is_retained_not_discarded(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    dispatcher = InboundDispatcher(processor)
    response = _response_message(recipient, sender)
    result = await dispatcher.dispatch_one(response)
    assert result.status == UNHANDLED
    assert result.detail == "REMOTE_RESPONSE"
    retained = await asyncio.wait_for(
        dispatcher.next_unhandled(), timeout=5
    )
    assert retained.message_id == response.message_id
    assert retained.request_id == response.request_id
    # Nothing was registered or executed for it.
    service = RequestService(db.sessions, _Keys())
    assert await service.get_request(response.request_id) is None


@pytest.mark.asyncio
async def test_other_types_retained_and_garbage_rejected(db):
    from backend.protocol.models import ApprovalRequestPayload
    import dataclasses

    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    dispatcher = InboundDispatcher(processor)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    other = dataclasses.replace(
        message,
        type=MessageType.APPROVAL_REQUEST,
        payload=ApprovalRequestPayload(
            request_ref=message.request_id,
            query="May I read your calendar?",
            requested_capabilities=["calendar.read"],
        ),
    )
    result = await dispatcher.dispatch_one(other)
    assert result.status == UNHANDLED
    assert (await asyncio.wait_for(
        dispatcher.next_unhandled(), timeout=5)).message_id == (
        other.message_id
    )
    garbage = await dispatcher.dispatch_one(object())
    assert garbage.status == REJECTED
    assert garbage.detail == "not-a-message"


@pytest.mark.asyncio
async def test_rejected_query_reports_typed_reason(db):
    import dataclasses

    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    dispatcher = InboundDispatcher(processor)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    tampered = dataclasses.replace(
        message,
        payload=dataclasses.replace(message.payload, query="PWNED"),
    )
    result = await dispatcher.dispatch_one(tampered)
    assert result.status == REJECTED
    assert result.detail == "InboundInvalidSignature"
    service = RequestService(db.sessions, _Keys())
    assert await service.get_request(message.request_id) is None


@pytest.mark.asyncio
async def test_duplicate_delivery_registers_once(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    dispatcher = InboundDispatcher(processor)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    first = await dispatcher.dispatch_one(message)
    second = await dispatcher.dispatch_one(message)
    assert (first.status, first.detail) == (HANDLED, "REGISTERED")
    assert (second.status, second.detail) == (HANDLED, "DUPLICATE")
    service = RequestService(db.sessions, _Keys())
    assert (await service.get_request(message.request_id)).status == (
        "WAITING_APPROVAL"
    )


@pytest.mark.asyncio
async def test_consumer_lifecycle_and_single_ownership(db):
    sender, recipient = create_identity(), create_identity()
    await seed_recipient_side(db, sender, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    dispatcher = InboundDispatcher(processor)
    source: asyncio.Queue = asyncio.Queue()
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    response = _response_message(recipient, sender)

    async def receive():
        return await source.get()

    task = dispatcher.start(receive)
    assert dispatcher.running
    with pytest.raises(DispatcherAlreadyRunning):
        dispatcher.start(receive)
    await source.put(message)
    await source.put(response)
    service = RequestService(db.sessions, _Keys())
    for _ in range(100):
        seen = await service.get_request(message.request_id)
        if seen is not None:
            break
        await asyncio.sleep(0.1)
    assert seen is not None
    assert seen.status == "WAITING_APPROVAL"
    retained = await asyncio.wait_for(
        dispatcher.next_unhandled(), timeout=10
    )
    assert retained.message_id == response.message_id
    await dispatcher.stop()
    assert not dispatcher.running
    assert task.done()
    await dispatcher.stop()  # idempotent


@pytest.mark.asyncio
async def test_from_client_binding_leaves_client_untouched(db):
    sender, recipient = create_identity(), create_identity()
    processor = InboundProcessor(db.sessions, recipient.agent_id)

    received = []

    class _FakeClient:
        def __init__(self):
            self.calls = 0

        async def receive_inbound(self, timeout=None):
            self.calls += 1
            return received.pop(0)

    client = _FakeClient()
    dispatcher, receive = InboundDispatcher.from_client(client, processor)
    message = signed_query(
        sender, recipient, request_id=unique("req"), message_id=unique("msg")
    )
    await seed_recipient_side(db, sender, recipient)
    received.append(message)
    result = await dispatcher.dispatch_one(await receive())
    assert result.status == HANDLED
    assert client.calls == 1
