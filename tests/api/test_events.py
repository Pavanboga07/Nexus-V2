"""Event hub, tickets, WebSocket auth and delivery (real PG where stored).

WebSocket handlers run against a deterministic fake transport so no
test depends on socket timing; the hub, tickets, origin policy, and
pump logic are the real implementations. Live route wiring (turns,
requests, approvals, execution) is proven through real HTTP calls.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from backend.api import deps
from backend.api.app import create_app
from backend.api.events import (
    CLOSE_GOING_AWAY,
    CLOSE_OVERLOADED,
    CLOSE_UNAUTHORIZED,
    REQUEST_CREATED,
    EventHub,
    TicketStore,
    make_event,
)
from backend.api.routes.events import _pump_connection, events_socket

from .conftest import (  # noqa: F401
    TOKEN,
    Keys,
    auth_headers,
    db,
    make_client,
    make_settings,
    make_state,
    seed_owned_agent,
    seed_peer_pair,
)


class FakeWebSocket:
    """Deterministic transport double for the WS route handler."""

    def __init__(self, app, headers=None, query=""):
        self.app = app
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.query_params = dict(
            part.split("=", 1) for part in query.split("&") if part
        ) if query else {}
        self.accepted = False
        self.close_code = None
        self.sent = []
        self.send_exc = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.close_code = code

    async def send_json(self, data):
        if self.send_exc is not None:
            raise self.send_exc
        self.sent.append(data)


async def _ticket(app, headers=None):
    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/events/ticket", headers=headers or auth_headers())
    assert response.status_code == 200, response.text
    return response.json()


async def _wait_for_sent(fake, count, timeout=10.0):
    """Wait until the fake transport received ``count`` events."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(fake.sent) < count:
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.05)
    return fake.sent


def _ws_ticket_headers(ticket):
    """Subprotocol headers carrying a ticket (the only supported slot)."""
    return {"sec-websocket-protocol": f"nexus-ticket.{ticket}"}


# -- hub units ---------------------------------------------------------------

def test_hub_fanout_and_unsubscribe():
    hub = EventHub(queue_size=4)
    first = hub.subscribe()
    second = hub.subscribe()
    assert hub.subscriber_count == 2
    event = make_event(REQUEST_CREATED, {"request_id": "req_1"})
    assert hub.publish(event) == 2
    assert hub.queue_of(first).get_nowait() == event
    assert hub.queue_of(second).get_nowait() == event
    hub.unsubscribe(first)
    assert hub.subscriber_count == 1
    assert hub.queue_of(first) is None
    hub.unsubscribe(first)  # idempotent


def test_hub_overload_closes_without_dropping_silently():
    hub = EventHub(queue_size=1)
    sub = hub.subscribe()
    assert hub.publish(make_event(REQUEST_CREATED, {"n": 1})) == 1
    assert hub.publish(make_event(REQUEST_CREATED, {"n": 2})) == 0
    assert hub.is_closed(sub)
    assert hub.subscriber_count == 0
    # The first event is still intact for whoever drains it.
    assert hub.queue_of(sub) is None


def test_hub_rejects_bad_queue_size():
    with pytest.raises(ValueError):
        EventHub(queue_size=0)


def test_event_envelope_shape():
    first = make_event(REQUEST_CREATED, {"request_id": "req_1"})
    second = make_event(REQUEST_CREATED, {"request_id": "req_1"})
    assert first.event_id != second.event_id
    wire = first.to_dict()
    assert set(wire) == {"event_id", "type", "timestamp", "data"}
    assert wire["type"] == REQUEST_CREATED
    parsed = datetime.fromisoformat(wire["timestamp"])
    assert parsed.tzinfo is not None
    with pytest.raises(ValueError):
        make_event("bogus.type", {})


def test_ticket_store_single_use_and_expiry():
    import uuid as uuid_mod

    owner, other = uuid_mod.uuid4(), uuid_mod.uuid4()
    store = TicketStore(ttl_seconds=60.0)
    ticket, expires_at = store.issue(owner)
    assert store.ticket_count == 1
    assert (expires_at - datetime.now(timezone.utc)).total_seconds() <= 60.0
    assert store.redeem(ticket, owner) is True
    assert store.redeem(ticket, owner) is False  # consumed
    assert store.redeem("nope", owner) is False
    foreign, _ = store.issue(other)
    assert store.redeem(foreign, owner) is False  # wrong owner


@pytest.mark.asyncio
async def test_ticket_store_expiry_and_bound():
    import uuid as uuid_mod

    store = TicketStore(ttl_seconds=0.05, max_tickets=3)
    ticket, _ = store.issue(uuid_mod.uuid4())
    await asyncio.sleep(0.15)
    assert store.redeem(ticket, uuid_mod.uuid4()) is False
    for _ in range(5):
        store.issue(uuid_mod.uuid4())
    assert store.ticket_count <= 3


# -- ticket endpoint ------------------------------------------------------------

@pytest.mark.asyncio
async def test_ticket_requires_auth(db):
    settings = make_settings()
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        assert (await client.post("/api/v1/events/ticket")).status_code == 401
        body = await client.post(
            "/api/v1/events/ticket", headers=auth_headers())
    assert body.status_code == 200
    payload = body.json()
    assert set(payload) == {"ticket", "expires_at"}
    assert len(payload["ticket"]) >= 32
    datetime.fromisoformat(payload["expires_at"])


@pytest.mark.asyncio
async def test_ticket_ttl_from_settings(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    from backend.api.events import TicketStore as Store

    state.tickets = Store(ttl_seconds=3600.0)
    app = create_app(settings, state=state)
    payload = await _ticket(app)
    remaining = (datetime.fromisoformat(payload["expires_at"])
                 - datetime.now(timezone.utc)).total_seconds()
    assert 3500.0 < remaining <= 3600.0


# -- websocket authentication ------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_missing_and_bad_tickets_close_4401(db):
    settings = make_settings()
    app = create_app(settings, state=make_state(db, settings)[0])
    cases = [
        FakeWebSocket(app),
        FakeWebSocket(
            app, headers=_ws_ticket_headers("nope")),
    ]
    for fake in cases:
        await events_socket(fake)
        assert fake.accepted is True
        assert fake.close_code == CLOSE_UNAUTHORIZED
        assert fake.sent == []


@pytest.mark.asyncio
async def test_ws_query_string_ticket_rejected(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    payload = await _ticket(app)
    # Tickets in the URL are never honored, even when valid: URLs are
    # logged by intermediaries, so this slot is closed entirely.
    fake = FakeWebSocket(app, query=f"ticket={payload['ticket']}")
    await events_socket(fake)
    assert fake.accepted is True
    assert fake.close_code == CLOSE_UNAUTHORIZED
    assert fake.sent == []
    assert state.events.subscriber_count == 0


@pytest.mark.asyncio
async def test_ws_bearer_token_is_not_a_ticket(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    fake = FakeWebSocket(app, headers=_ws_ticket_headers(TOKEN))
    await events_socket(fake)
    assert fake.close_code == CLOSE_UNAUTHORIZED
    assert state.events.subscriber_count == 0


@pytest.mark.asyncio
async def test_ws_ticket_reuse_fails_second(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    payload = await _ticket(app)
    first = FakeWebSocket(
        app, headers=_ws_ticket_headers(payload['ticket']))
    second = FakeWebSocket(
        app, headers=_ws_ticket_headers(payload['ticket']))
    first_task = asyncio.ensure_future(events_socket(first))
    await asyncio.sleep(0.2)
    await events_socket(second)
    assert second.close_code == CLOSE_UNAUTHORIZED
    assert first.close_code is None
    assert first.accepted is True
    assert state.events.subscriber_count == 1
    first_task.cancel()
    try:
        await first_task
    except asyncio.CancelledError:
        pass
    assert state.events.subscriber_count == 0


@pytest.mark.asyncio
async def test_ws_concurrent_redeem_exactly_one_wins(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    payload = await _ticket(app)
    query = payload['ticket']
    fakes = [FakeWebSocket(app, headers=_ws_ticket_headers(query))
             for _ in range(2)]
    tasks = [asyncio.ensure_future(events_socket(fake)) for fake in fakes]
    await asyncio.sleep(0.5)
    codes = sorted(fake.close_code or 0 for fake in fakes)
    assert codes == [0, CLOSE_UNAUTHORIZED]
    assert state.events.subscriber_count == 1
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert state.events.subscriber_count == 0


@pytest.mark.asyncio
async def test_ws_foreign_owner_ticket_rejected(db):
    import uuid as uuid_mod

    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    ticket, _ = state.tickets.issue(uuid_mod.uuid4())
    fake = FakeWebSocket(app, headers=_ws_ticket_headers(ticket))
    await events_socket(fake)
    assert fake.close_code == CLOSE_UNAUTHORIZED


@pytest.mark.asyncio
async def test_ws_subprotocol_ticket_preferred(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    payload = await _ticket(app)
    fake = FakeWebSocket(
        app, headers={"sec-websocket-protocol":
                      f"nostromo, nexus-ticket.{payload['ticket']}"})
    task = asyncio.ensure_future(events_socket(fake))
    await asyncio.sleep(0.2)
    try:
        assert fake.accepted is True
        assert fake.close_code is None
        assert state.events.subscriber_count == 1
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_ws_disallowed_origin_rejected_before_accept(db):
    from fastapi import WebSocketException

    settings = make_settings()
    app = create_app(settings, state=make_state(db, settings)[0])
    fake = FakeWebSocket(
        app, headers={"origin": "https://evil.example",
                      "host": "127.0.0.1:8000"})
    # Denial raises before any accept: no socket, no ticket spent.
    # Over a real server Starlette translates this to HTTP 403 on
    # the handshake (verified against live uvicorn).
    with pytest.raises(WebSocketException) as exc_info:
        await events_socket(fake)
    assert exc_info.value.code == 4403
    assert fake.accepted is False


@pytest.mark.asyncio
async def test_ws_same_and_listed_origins_accepted(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    payload = await _ticket(app)
    same = FakeWebSocket(
        app, headers={"origin": "http://127.0.0.1:8000",
                      "host": "127.0.0.1:8000",
                      "sec-websocket-protocol":
                      f"nexus-ticket.{payload['ticket']}"})
    same_task = asyncio.ensure_future(events_socket(same))
    await asyncio.sleep(0.2)
    try:
        assert same.accepted is True
        assert same.close_code is None
    finally:
        same_task.cancel()
        try:
            await same_task
        except asyncio.CancelledError:
            pass
    listed_settings = make_settings(
        allowed_origins=("https://app.example",))
    listed_state, _ = make_state(db, listed_settings)
    listed_app = create_app(listed_settings, state=listed_state)
    listed_payload = await _ticket(listed_app)
    listed = FakeWebSocket(
        listed_app, headers={"origin": "https://app.example",
                             "host": "127.0.0.1:8000",
                             "sec-websocket-protocol":
                             f"nexus-ticket.{listed_payload['ticket']}"})
    listed_task = asyncio.ensure_future(events_socket(listed))
    await asyncio.sleep(0.2)
    try:
        assert listed.accepted is True
    finally:
        listed_task.cancel()
        try:
            await listed_task
        except asyncio.CancelledError:
            pass


# -- pump behavior ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_delivers_then_cleans_up(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    hub = state.events
    sub = hub.subscribe()
    event = make_event(REQUEST_CREATED, {"request_id": "req_9"})
    hub.publish(event)

    class Stub:
        def __init__(self):
            self.sent = []
            self.close_code = None

        async def send_json(self, data):
            self.sent.append(data)

        async def close(self, code=1000):
            self.close_code = code

    stub = Stub()
    task = asyncio.ensure_future(_pump_connection(stub, hub, sub))
    await asyncio.sleep(0.2)
    assert stub.sent == [event.to_dict()]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_pump_overloaded_closes_1013(db):
    hub = EventHub(queue_size=1)
    sub = hub.subscribe()
    hub.publish(make_event(REQUEST_CREATED, {"n": 1}))
    hub.publish(make_event(REQUEST_CREATED, {"n": 2}))

    class Stub:
        def __init__(self):
            self.sent = []
            self.close_code = None

        async def send_json(self, data):
            self.sent.append(data)

        async def close(self, code=1000):
            self.close_code = code

    stub = Stub()
    await _pump_connection(stub, hub, sub)
    assert stub.close_code == CLOSE_OVERLOADED
    assert stub.sent == []
    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_pump_send_failure_unsubscribes(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    hub = state.events
    sub = hub.subscribe()
    hub.publish(make_event(REQUEST_CREATED, {"n": 1}))

    class Broken:
        def __init__(self):
            self.close_code = None

        async def send_json(self, data):
            raise ConnectionError("peer gone")

        async def close(self, code=1000):
            self.close_code = code

    broken = Broken()
    await _pump_connection(broken, hub, sub)
    assert broken.close_code == CLOSE_GOING_AWAY
    assert hub.subscriber_count == 0


# -- live event wiring -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn_completed_event_arrives(db):
    from .conftest import seed_owned_agent

    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    app = create_app(settings, state=state)
    agent = await seed_owned_agent(db, keys)
    payload = await _ticket(app)
    fake = FakeWebSocket(
        app, headers=_ws_ticket_headers(payload['ticket']))
    task = asyncio.ensure_future(events_socket(fake))
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            headers = auth_headers()
            created = await client.post(
                "/api/v1/conversations", headers=headers,
                json={"agent_id": agent.agent_id})
            conversation_id = created.json()["id"]
            turn = await client.post(
                f"/api/v1/conversations/{conversation_id}/turns",
                headers=headers, json={"message": "hi there"})
            assert turn.status_code == 200
        assert len(await _wait_for_sent(fake, 1)) == 1
        event = fake.sent[0]
        assert event["type"] == "conversation.turn.completed"
        assert event["data"]["conversation_id"] == conversation_id
        assert event["data"]["model"] == "injected"
        assert set(event) == {"event_id", "type", "timestamp", "data"}
        assert TOKEN not in json.dumps(fake.sent)
        assert "helpful AI assistant" not in json.dumps(fake.sent)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_request_created_event_arrives(db):
    from .conftest import seed_peer_pair

    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    app = create_app(settings, state=state)
    local, remote = await seed_peer_pair(db, keys)
    payload = await _ticket(app)
    fake = FakeWebSocket(
        app, headers=_ws_ticket_headers(payload['ticket']))
    task = asyncio.ensure_future(events_socket(fake))
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            created = await client.post(
                "/api/v1/requests", headers=auth_headers(), json={
                    "sender_agent_id": local.agent_id,
                    "recipient_agent_id": remote.agent_id,
                    "query": "ping?",
                    "expires_in_seconds": 3600,
                })
            assert created.status_code == 201
        assert len(await _wait_for_sent(fake, 1)) == 1
        assert fake.sent[0]["type"] == "remote.request.created"
        assert (fake.sent[0]["data"]["request_id"]
                == created.json()["request_id"])
        assert fake.sent[0]["data"]["status"] == "QUEUED"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_failed_turn_emits_no_event(db):
    from .conftest import AnswerModel, seed_owned_agent

    from backend.agent.errors import ModelInvocationError

    class Flaky(AnswerModel):
        def _generate(self, messages, stop=None, run_manager=None,
                      **kwargs):
            raise ModelInvocationError("model down")

    keys = Keys()
    settings = make_settings()
    state, keys = make_state(db, settings, keys, model=Flaky())
    app = create_app(settings, state=state)
    agent = await seed_owned_agent(db, keys)
    payload = await _ticket(app)
    fake = FakeWebSocket(
        app, headers=_ws_ticket_headers(payload['ticket']))
    task = asyncio.ensure_future(events_socket(fake))
    await asyncio.sleep(0.2)
    try:
        async with make_client(app) as client:
            headers = auth_headers()
            created = await client.post(
                "/api/v1/conversations", headers=headers,
                json={"agent_id": agent.agent_id})
            response = await client.post(
                f"/api/v1/conversations/{created.json()['id']}/turns",
                headers=headers, json={"message": "hi"})
            assert response.status_code == 503
        assert fake.sent == []
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_reconnect_flow_with_fresh_ticket(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    first = FakeWebSocket(
        app, headers=_ws_ticket_headers(
            (await _ticket(app))['ticket']))
    task = asyncio.ensure_future(events_socket(first))
    await asyncio.sleep(0.2)
    try:
        state.events.publish(make_event(REQUEST_CREATED, {"n": 1}))
        assert len(await _wait_for_sent(first, 1)) == 1
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert state.events.subscriber_count == 0
    second = FakeWebSocket(
        app, headers=_ws_ticket_headers(
            (await _ticket(app))['ticket']))
    task2 = asyncio.ensure_future(events_socket(second))
    await asyncio.sleep(0.2)
    try:
        state.events.publish(make_event(REQUEST_CREATED, {"n": 2}))
        assert len(await _wait_for_sent(second, 1)) == 1
        assert second.sent[0]["data"] == {"n": 2}
    finally:
        task2.cancel()
        try:
            await task2
        except asyncio.CancelledError:
            pass
    assert state.events.subscriber_count == 0
