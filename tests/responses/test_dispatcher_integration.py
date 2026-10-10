"""Receiver/dispatcher integration: attach drain, live dispatch (real PG).

The receiver registers exactly once on the single-consumer dispatcher;
pre-registration backlog is drained without loss or reordering of
unrelated mail, and the running loop survives garbage and failures.
"""

import asyncio
import dataclasses

import pytest

from backend.inbound import (
    HANDLED,
    REJECTED,
    UNHANDLED,
    HandlerAlreadyRegistered,
    InboundDispatcher,
    InboundProcessor,
)
from backend.protocol.models import ApprovalRequestPayload
from backend.protocol.types import MessageType
from backend.requests.service import RequestService
from backend.responses import REGISTERED

from .conftest import (
    Keys,
    live_times,
    make_receiver,
    response_messages,
    seed_originator_sent,
    signed_response,
)


class _ReadKeys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


def _other_message(response):
    return dataclasses.replace(
        response,
        message_id=response.message_id + "_other",
        type=MessageType.APPROVAL_REQUEST,
        payload=ApprovalRequestPayload(
            request_ref=response.request_id,
            query="May I?",
            requested_capabilities=["calendar.read"],
        ),
    )


async def _request(db, request_id):
    return await RequestService(db.sessions, _ReadKeys()).get_request(
        request_id)


@pytest.mark.asyncio
async def test_attach_drains_backlog_preserving_others(db):
    from backend.requests.service import RequestService

    keys = Keys()
    originator, remote, request_a = await seed_originator_sent(db, keys)
    created0, expires0 = live_times()
    service = RequestService(db.sessions, keys)
    extra = await service.create_remote_request(
        originator.agent_id, remote.agent_id, "second?", [],
        expires0, now=created0,
    )
    request_b = (await service.mark_sent(extra.request_id)).request_id
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, originator.agent_id))
    created, _ = live_times()
    resp_a = signed_response(
        remote, originator.agent_id, request_a, created=created)
    other = _other_message(resp_a)
    resp_b = signed_response(
        remote, originator.agent_id, request_b, created=created)
    assert (await dispatcher.dispatch_one(resp_a)).status == UNHANDLED
    assert (await dispatcher.dispatch_one(other)).status == UNHANDLED
    assert (await dispatcher.dispatch_one(resp_b)).status == UNHANDLED

    receiver = make_receiver(db, keys, originator.agent_id)
    await receiver.attach(dispatcher)
    assert (await _request(db, request_a)).status == "COMPLETED"
    assert (await _request(db, request_b)).status == "COMPLETED"
    # Only the unrelated message remains, exactly once.
    kept = await asyncio.wait_for(dispatcher.next_unhandled(), timeout=5)
    assert kept.message_id == other.message_id
    assert dispatcher.unhandled.empty()
    # Re-attaching the same receiver is rejected, not duplicated.
    with pytest.raises(HandlerAlreadyRegistered):
        await receiver.attach(dispatcher)
    await receiver.detach(dispatcher)


@pytest.mark.asyncio
async def test_live_dispatch_to_attached_receiver(db):
    keys = Keys()
    originator, remote, request_id = await seed_originator_sent(db, keys)
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, originator.agent_id))
    receiver = make_receiver(db, keys, originator.agent_id)
    await receiver.attach(dispatcher)
    source: asyncio.Queue = asyncio.Queue()
    task = dispatcher.start(source.get)
    try:
        assert dispatcher.running
        created, _ = live_times()
        await source.put(signed_response(
            remote, originator.agent_id, request_id, created=created))
        for _ in range(200):
            seen = await _request(db, request_id)
            if seen is not None and seen.status == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert seen is not None and seen.status == "COMPLETED"
        # Garbage neither kills the loop nor touches the request.
        await source.put(object())
        await asyncio.sleep(0.3)
        assert dispatcher.running
        assert (await _request(db, request_id)).status == "COMPLETED"
    finally:
        await dispatcher.stop()
    assert not dispatcher.running
    assert task.done()


@pytest.mark.asyncio
async def test_cancel_releases_ownership_with_handler(db):
    keys = Keys()
    originator, _, _ = await seed_originator_sent(db, keys)
    dispatcher = InboundDispatcher(InboundProcessor(
        db.sessions, originator.agent_id))
    receiver = make_receiver(db, keys, originator.agent_id)
    await receiver.attach(dispatcher)
    source: asyncio.Queue = asyncio.Queue()
    task = dispatcher.start(source.get)
    assert dispatcher.running
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert not dispatcher.running
    # Ownership released: a replacement dispatcher can start.
    second = InboundDispatcher(InboundProcessor(
        db.sessions, originator.agent_id))
    task2 = second.start(source.get)
    assert second.running
    await second.stop()
    assert task2.done()
    await receiver.detach(dispatcher)
