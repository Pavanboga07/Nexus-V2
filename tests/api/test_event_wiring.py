"""Event-producer wiring: every event type proven from real transitions.

Each test drives an actual application path — processor, receiver,
dispatcher, or HTTP route — and asserts the exact published events
(and only those events) on a subscribed hub. Rollbacks, duplicates,
rejections, and failures must stay silent.
"""

import asyncio
import dataclasses
from unittest import mock

import pytest

from backend.api.app import create_app
from backend.api.events import (
    APPROVAL_REQUIRED,
    EXECUTION_UPDATED,
    REQUEST_UPDATED,
    RESPONSE_RECEIVED,
    EventHub,
    make_event,
)
from backend.identity.identity import create_identity
from backend.inbound import HANDLED, InboundDispatcher, InboundProcessor
from backend.inbound.errors import InboundError
from backend.persistence.database import session_scope
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.messages import (
    MessageRepository as Messages,
)
from backend.persistence.repositories.peers import PeerRepository
from backend.requests.service import RequestService
from backend.responses import REGISTERED, ResponseReceiver

from .conftest import (  # noqa: F401
    Keys,
    _pub,
    auth_headers,
    db,
    live_times,
    make_client,
    make_settings,
    make_state,
    seed_inbound_request,
    seed_peer_pair,
    unique,
)
from .test_events import FakeWebSocket, _ticket, _wait_for_sent
from tests.inbound.conftest import seed_recipient_side, signed_query
from tests.responses.conftest import signed_response


class _ReadKeys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


async def _drain_nowait(hub, sub):
    """Collect everything currently queued (deterministic, no sleeps)."""
    got = []
    while True:
        try:
            got.append(hub.queue_of(sub).get_nowait())
        except asyncio.QueueEmpty:
            break
    return got


async def _sent_request(db, keys):
    """Originator request with durable dispatch evidence (SENT)."""
    local, remote = await seed_peer_pair(db, keys)
    created, expires = live_times()
    service = RequestService(db.sessions, keys)
    record = await service.create_remote_request(
        local.agent_id, remote.agent_id, "ping?", [], expires, now=created)
    await service.mark_sent(record.request_id)
    return local, remote, record.request_id


def _spawn_ws(app, ticket):
    """Connect a fake transport; return (fake, task) already pumping."""
    fake = FakeWebSocket(
        app, headers={"sec-websocket-protocol": f"nexus-ticket.{ticket}"})
    return fake, asyncio.ensure_future(_ws_handler(app, fake))


async def _ws_handler(app, fake):
    from backend.api.routes.events import events_socket

    await events_socket(fake)


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# -- inbound registration -------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_required_on_registration(db):
    keys = Keys()
    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    await seed_recipient_side(db, sender, recipient)
    hub = EventHub()
    sub = hub.subscribe()
    processor = InboundProcessor(
        db.sessions, recipient.agent_id,
        on_registered=lambda message: hub.publish(
            make_event(APPROVAL_REQUIRED, {
                "request_id": message.request_id,
                "sender": message.sender,
                "recipient": message.recipient,
            })),
    )
    created, expires = live_times()
    message = signed_query(
        sender, recipient, request_id=unique("req"),
        message_id=unique("msg"), created_at=created,
        expires_at=expires,
    )
    assert (await processor.process(message)).status == "REGISTERED"
    got = await _drain_nowait(hub, sub)
    assert len(got) == 1
    assert got[0].type == APPROVAL_REQUIRED
    assert got[0].data == {
        "request_id": message.request_id,
        "sender": sender.agent_id,
        "recipient": recipient.agent_id,
    }


@pytest.mark.asyncio
async def test_approval_required_duplicate_and_rejected_silent(db):
    keys = Keys()
    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    await seed_recipient_side(db, sender, recipient)
    hub = EventHub()
    calls = []
    sub = hub.subscribe()

    def _record(message):
        calls.append(message.message_id)
        hub.publish(make_event(APPROVAL_REQUIRED, {
            "request_id": message.request_id,
            "sender": message.sender,
            "recipient": message.recipient,
        }))

    processor = InboundProcessor(
        db.sessions, recipient.agent_id, on_registered=_record)
    created, expires = live_times()
    message = signed_query(
        sender, recipient, request_id=unique("req"),
        message_id=unique("msg"), created_at=created,
        expires_at=expires,
    )
    assert (await processor.process(message)).status == "REGISTERED"
    assert (await processor.process(message)).status == "DUPLICATE"
    assert calls == [message.message_id]
    assert len(await _drain_nowait(hub, sub)) == 1
    tampered = signed_query(
        sender, recipient, request_id=unique("req"),
        message_id=unique("msg"), created_at=created,
        expires_at=expires,
    )
    bad = dataclasses.replace(
        tampered, payload=dataclasses.replace(
            tampered.payload, query="PWNED"))
    with pytest.raises(InboundError):
        await processor.process(bad)
    assert calls == [message.message_id]
    assert await _drain_nowait(hub, sub) == []


@pytest.mark.asyncio
async def test_approval_required_rollback_silent(db):
    keys = Keys()
    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    await seed_recipient_side(db, sender, recipient)
    hub = EventHub()
    sub = hub.subscribe()
    processor = InboundProcessor(
        db.sessions, recipient.agent_id,
        on_registered=lambda message: hub.publish(
            make_event(APPROVAL_REQUIRED, {
                "request_id": message.request_id})),
    )
    created, expires = live_times()
    message = signed_query(
        sender, recipient, request_id=unique("req"),
        message_id=unique("msg"), created_at=created,
        expires_at=expires,
    )
    with mock.patch.object(
        Messages, "create", side_effect=RuntimeError("store down"),
    ):
        with pytest.raises(Exception):
            await processor.process(message)
    assert await _drain_nowait(hub, sub) == []
    assert (await processor.process(message)).status == "REGISTERED"
    assert len(await _drain_nowait(hub, sub)) == 1


@pytest.mark.asyncio
async def test_approval_required_callback_failure_contained(db):
    keys = Keys()
    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    await seed_recipient_side(db, sender, recipient)

    def _boom(message):
        raise RuntimeError("notify down")

    processor = InboundProcessor(
        db.sessions, recipient.agent_id, on_registered=_boom)
    created, expires = live_times()
    message = signed_query(
        sender, recipient, request_id=unique("req"),
        message_id=unique("msg"), created_at=created,
        expires_at=expires,
    )
    outcome = await processor.process(message)
    assert outcome.status == "REGISTERED"
    service = RequestService(db.sessions, _ReadKeys())
    stored = await service.get_request(message.request_id)
    assert stored is not None and stored.status == "WAITING_APPROVAL"


# -- response reception ----------------------------------------------------------

@pytest.mark.asyncio
async def test_response_received_on_accept(db):
    keys = Keys()
    originator, remote, request_id = await _sent_request(db, keys)
    hub = EventHub()
    seen = []
    sub = hub.subscribe()

    def _record(outcome):
        seen.append(outcome)
        hub.publish(make_event(RESPONSE_RECEIVED, {
            "request_id": outcome.request_id,
            "message_id": outcome.response_message_id,
            "status": outcome.detail,
        }))
        hub.publish(make_event(REQUEST_UPDATED, {
            "request_id": outcome.request_id,
            "status": outcome.detail,
        }))

    receiver = ResponseReceiver(
        db.sessions, originator.agent_id, keys, on_registered=_record)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    assert (await receiver.handle(response, now=created)).status == REGISTERED
    assert len(seen) == 1
    got = await _drain_nowait(hub, sub)
    assert [event.type for event in got] == [
        RESPONSE_RECEIVED, REQUEST_UPDATED]
    assert got[0].data == {
        "request_id": request_id,
        "message_id": response.message_id,
        "status": "COMPLETED",
    }
    assert got[1].data == {"request_id": request_id, "status": "COMPLETED"}


@pytest.mark.asyncio
async def test_response_duplicate_and_failure_silent(db):
    keys = Keys()
    originator, remote, request_id = await _sent_request(db, keys)
    hub = EventHub()
    calls = []
    sub = hub.subscribe()
    receiver = ResponseReceiver(
        db.sessions, originator.agent_id, keys,
        on_registered=lambda outcome: (
            calls.append(outcome),
            hub.publish(make_event(RESPONSE_RECEIVED, {
                "request_id": outcome.request_id})),
        ))
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    assert (await receiver.handle(response, now=created)).status == REGISTERED
    from backend.responses import DUPLICATE

    assert (await receiver.handle(response, now=created)).status == DUPLICATE
    assert len(calls) == 1
    assert len(await _drain_nowait(hub, sub)) == 1
    created2, expires2 = live_times()
    service = RequestService(db.sessions, keys)
    extra = await service.create_remote_request(
        originator.agent_id, remote.agent_id, "second?", [], expires2,
        now=created2,
    )
    other_id = (await service.mark_sent(extra.request_id)).request_id
    bad = signed_response(
        remote, originator.agent_id, other_id, created=created)
    with mock.patch.object(
        Messages, "create", side_effect=RuntimeError("store down"),
    ):
        with pytest.raises(RuntimeError):
            await receiver.handle(bad, now=created)
    assert len(calls) == 1
    assert await _drain_nowait(hub, sub) == []


@pytest.mark.asyncio
async def test_receiver_callback_failure_contained(db):
    keys = Keys()
    originator, remote, request_id = await _sent_request(db, keys)
    hub = EventHub()
    sub = hub.subscribe()

    def _boom(outcome):
        raise RuntimeError("notify down")

    receiver = ResponseReceiver(
        db.sessions, originator.agent_id, keys, on_registered=_boom)
    created, _ = live_times()
    response = signed_response(
        remote, originator.agent_id, request_id, created=created)
    outcome = await receiver.handle(response, now=created)
    assert outcome.status == REGISTERED
    service = RequestService(db.sessions, _ReadKeys())
    assert (await service.get_request(request_id)).status == "COMPLETED"
    assert await _drain_nowait(hub, sub) == []


@pytest.mark.asyncio
async def test_dispatcher_survives_callback_failure(db):
    keys = Keys()
    originator, remote, request_id = await _sent_request(db, keys)
    processor = InboundProcessor(db.sessions, originator.agent_id)
    dispatcher = InboundDispatcher(processor)
    hub = EventHub()
    sub = hub.subscribe()

    def _boom(outcome):
        raise RuntimeError("notify down")

    receiver = ResponseReceiver(
        db.sessions, originator.agent_id, keys, on_registered=_boom)
    await receiver.attach(dispatcher)
    try:
        created, _ = live_times()
        response = signed_response(
            remote, originator.agent_id, request_id, created=created)
        result = await dispatcher.dispatch_one(response)
        from backend.inbound import HANDLED

        assert result.status == HANDLED
        service = RequestService(db.sessions, _ReadKeys())
        assert (await service.get_request(request_id)).status == "COMPLETED"
        assert await _drain_nowait(hub, sub) == []
    finally:
        await receiver.detach(dispatcher)


# -- approval and rejection over HTTP ----------------------------------------------

@pytest.mark.asyncio
async def test_approve_reject_publish_exact_updates(db):
    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    app = create_app(settings, state=state)
    _, _, first = await seed_inbound_request(db, keys)
    _, _, second = await seed_inbound_request(db, keys)
    fake, task = _spawn_ws(app, (await _ticket(app))["ticket"])
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            headers = auth_headers()
            assert (await client.post(
                f"/api/v1/requests/{first}/approve",
                headers=headers, json={})).status_code == 200
            assert (await client.post(
                f"/api/v1/requests/{second}/reject",
                headers=headers, json={})).status_code == 200
        events = await _wait_for_sent(fake, 2)
        by_id = {event["data"]["request_id"]: event for event in events
                 if "request_id" in event["data"]}
        assert by_id[first]["type"] == "remote.request.updated"
        assert by_id[first]["data"] == {
            "request_id": first, "status": "APPROVED"}
        assert by_id[second]["type"] == "remote.request.updated"
        assert by_id[second]["data"] == {
            "request_id": second, "status": "REJECTED"}
    finally:
        await _cancel(task)
    assert state.events.subscriber_count == 0


@pytest.mark.asyncio
async def test_conflicting_decision_emits_nothing_new(db):
    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    app = create_app(settings, state=state)
    _, _, request_id = await seed_inbound_request(db, keys)
    fake, task = _spawn_ws(app, (await _ticket(app))["ticket"])
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            headers = auth_headers()
            assert (await client.post(
                f"/api/v1/requests/{request_id}/approve",
                headers=headers, json={})).status_code == 200
        assert len(await _wait_for_sent(fake, 1)) == 1
        async with make_client(app) as client:
            again = await client.post(
                f"/api/v1/requests/{request_id}/approve",
                headers=auth_headers(), json={})
            assert again.status_code == 409
        await asyncio.sleep(0.3)
        assert len(fake.sent) == 1
    finally:
        await _cancel(task)
    assert state.events.subscriber_count == 0


# -- execution over HTTP -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_emits_outcome_and_update(db):
    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    app = create_app(settings, state=state)
    local, _, request_id = await seed_inbound_request(db, keys)
    service = RequestService(db.sessions, keys)
    await service.approve(request_id, local.agent_id)
    fake, task = _spawn_ws(app, (await _ticket(app))["ticket"])
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            done = await client.post(
                f"/api/v1/requests/{request_id}/execute",
                headers=auth_headers())
            assert done.status_code == 200
            assert done.json()["status"] == "COMPLETED"
        events = await _wait_for_sent(fake, 2)
        by_type = {}
        for event in events:
            by_type.setdefault(event["type"], []).append(event)
        assert set(by_type) == {"execution.updated", "remote.request.updated"}
        execution = by_type["execution.updated"][0]
        assert execution["data"]["request_id"] == request_id
        assert execution["data"]["status"] == "COMPLETED"
        assert execution["data"]["executed"] is True
        assert execution["data"]["response_message_id"] is not None
        updated = by_type["remote.request.updated"][0]
        assert updated["data"] == {
            "request_id": request_id, "status": "COMPLETED"}
    finally:
        await _cancel(task)
    assert state.events.subscriber_count == 0


@pytest.mark.asyncio
async def test_execute_in_progress_emits_no_new_start(db):
    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    model = state.runtime._model
    app = create_app(settings, state=state)
    local, _, request_id = await seed_inbound_request(db, keys)
    service = RequestService(db.sessions, keys)
    await service.approve(request_id, local.agent_id)
    await service.mark_processing(request_id)
    fake, task = _spawn_ws(app, (await _ticket(app))["ticket"])
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            response = await client.post(
                f"/api/v1/requests/{request_id}/execute",
                headers=auth_headers())
            assert response.status_code == 200
            assert response.json()["status"] == "IN_PROGRESS"
        events = await _wait_for_sent(fake, 1)
        assert len(events) == 1
        assert events[0]["type"] == "execution.updated"
        assert events[0]["data"]["status"] == "IN_PROGRESS"
        assert events[0]["data"]["executed"] is False
        assert model.calls == 0
    finally:
        await _cancel(task)
    assert state.events.subscriber_count == 0


@pytest.mark.asyncio
async def test_execute_failed_outcome_pins_results(db):
    from .conftest import AnswerModel

    from backend.agent.errors import ModelInvocationError

    class Flaky(AnswerModel):
        def _generate(self, messages, stop=None, run_manager=None,
                      **kwargs):
            raise ModelInvocationError("model down")

    keys = Keys()
    settings = make_settings()
    state, keys = make_state(db, settings, keys, model=Flaky())
    app = create_app(settings, state=state)
    local, _, request_id = await seed_inbound_request(db, keys)
    service = RequestService(db.sessions, keys)
    await service.approve(request_id, local.agent_id)
    fake, task = _spawn_ws(app, (await _ticket(app))["ticket"])
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            done = await client.post(
                f"/api/v1/requests/{request_id}/execute",
                headers=auth_headers())
            assert done.status_code == 200
            assert done.json()["status"] == "FAILED"
        events = await _wait_for_sent(fake, 2)
        by_type = {}
        for event in events:
            by_type.setdefault(event["type"], []).append(event)
        assert by_type["execution.updated"][0]["data"]["status"] == "FAILED"
        assert by_type["remote.request.updated"][0]["data"] == {
            "request_id": request_id, "status": "FAILED"}
    finally:
        await _cancel(task)
    assert state.events.subscriber_count == 0


@pytest.mark.asyncio
async def test_execute_rejected_emits_nothing(db):
    from .conftest import AnswerModel

    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    model = state.runtime._model
    assert isinstance(model, AnswerModel)
    app = create_app(settings, state=state)
    _, _, request_id = await seed_inbound_request(db, keys)
    fake, task = _spawn_ws(app, (await _ticket(app))["ticket"])
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            denied = await client.post(
                f"/api/v1/requests/{request_id}/execute",
                headers=auth_headers())
            assert denied.status_code == 409
        await asyncio.sleep(0.3)
        assert fake.sent == []
        assert model.calls == 0
    finally:
        await _cancel(task)
    assert state.events.subscriber_count == 0


async def _ticket(app, headers=None):
    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/events/ticket", headers=headers or auth_headers())
    assert response.status_code == 200, response.text
    return response.json()


async def _wait_for_sent(fake, count, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(fake.sent) < count:
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.05)
    return fake.sent


def _spawn_ws(app, ticket):
    from tests.api.test_events import FakeWebSocket

    fake = FakeWebSocket(
        app, headers={"sec-websocket-protocol": f"nexus-ticket.{ticket}"})
    return fake, asyncio.ensure_future(_ws_handler(app, fake))


async def _ws_handler(app, fake):
    from backend.api.routes.events import events_socket

    await events_socket(fake)


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
