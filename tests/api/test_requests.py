"""Remote-request endpoint tests: lifecycle, ownership, errors (real PG)."""

import pytest

from backend.api.app import create_app

from .conftest import (  # noqa: F401
    Keys,
    auth_headers,
    db,
    live_times,
    make_client,
    make_settings,
    make_state,
    seed_inbound_request,
    seed_peer_pair,
)


def _app(db, settings=None, keys=None):
    settings = settings or make_settings()
    state, keys = make_state(db, settings, keys=keys)
    return create_app(settings, state=state), keys


@pytest.mark.asyncio
async def test_create_and_get_request(db):
    keys = Keys()
    app, _ = _app(db, settings := make_settings(), keys)
    local, remote = await seed_peer_pair(db, keys)
    async with make_client(app) as client:
        headers = auth_headers()
        created = await client.post(
            "/api/v1/requests", headers=headers, json={
                "sender_agent_id": local.agent_id,
                "recipient_agent_id": remote.agent_id,
                "query": "What is two plus two?",
                "requested_capabilities": ["calendar.read"],
                "expires_in_seconds": 3600,
            })
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "QUEUED"
        assert body["sender_agent_id"] == local.agent_id
        assert body["answer"] is None
        fetched = await client.get(
            f"/api/v1/requests/{body['request_id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["request_id"] == body["request_id"]


@pytest.mark.asyncio
async def test_create_rejects_unauthorized_and_unknown(db):
    import uuid as uuid_mod

    keys = Keys()
    app, _ = _app(db, settings := make_settings(), keys)
    local, remote = await seed_peer_pair(db, keys)
    stranger = await seed_peer_pair(db, keys, owner=uuid_mod.uuid4())
    stranger_local = stranger[0]
    payload = {
        "sender_agent_id": local.agent_id,
        "recipient_agent_id": remote.agent_id,
        "query": "hi",
        "expires_in_seconds": 3600,
    }
    async with make_client(app) as client:
        headers = auth_headers()
        # Sender owned by someone else: invisible.
        other = dict(payload, sender_agent_id=stranger_local.agent_id)
        assert (await client.post(
            "/api/v1/requests", headers=headers,
            json=other)).status_code == 404
        # Unknown recipient: no connected peer.
        unknown = dict(payload, recipient_agent_id="agent_unknown")
        response = await client.post(
            "/api/v1/requests", headers=headers, json=unknown)
        assert response.status_code in (404, 422)
        # Caller-supplied ids conflict honestly on reuse.
        from .conftest import unique as _unique

        reuse_id = _unique("req")
        first = await client.post(
            "/api/v1/requests", headers=headers, json=dict(
                payload, request_id=reuse_id))
        assert first.status_code == 201
        again = await client.post(
            "/api/v1/requests", headers=headers, json=dict(
                payload, request_id=reuse_id))
        assert again.status_code == 409
        assert again.json()["code"] == "ALREADY_EXISTS"
        # Malformed bodies never reach the service.
        assert (await client.post(
            "/api/v1/requests", headers=headers,
            json={"query": "hi"})).status_code == 422
        assert (await client.post(
            "/api/v1/requests", headers=headers, json=dict(
                payload, expires_in_seconds=5))).status_code == 422


@pytest.mark.asyncio
async def test_get_unknown_and_foreign_requests_404(db):
    import uuid as uuid_mod

    keys = Keys()
    app, _ = _app(db, settings := make_settings(), keys)
    await seed_peer_pair(db, keys)
    foreign = await seed_peer_pair(db, keys, owner=uuid_mod.uuid4())
    from backend.requests.service import RequestService

    service = RequestService(db.sessions, keys)
    created, expires = live_times()
    record = await service.create_remote_request(
        foreign[0].agent_id, foreign[1].agent_id, "private?", [],
        expires, now=created)
    async with make_client(app) as client:
        headers = auth_headers()
        assert (await client.get(
            "/api/v1/requests/req_nope",
            headers=headers)).status_code == 404
        response = await client.get(
            f"/api/v1/requests/{record.request_id}", headers=headers)
        assert response.status_code == 404
        assert response.json()["code"] == "UNKNOWN_REQUEST"


@pytest.mark.asyncio
async def test_list_requests_bounded(db):
    keys = Keys()
    app, _ = _app(db, settings := make_settings(), keys)
    local, remote = await seed_peer_pair(db, keys)
    async with make_client(app) as client:
        headers = auth_headers()
        for index in range(3):
            await client.post(
                "/api/v1/requests", headers=headers, json={
                    "sender_agent_id": local.agent_id,
                    "recipient_agent_id": remote.agent_id,
                    "query": f"q{index}",
                    "expires_in_seconds": 3600,
                })
        outbound = await client.get(
            "/api/v1/requests",
            params={"role": "outbound", "limit": 2}, headers=headers)
        assert outbound.status_code == 200
        assert len(outbound.json()) == 2
        queued = await client.get(
            "/api/v1/requests",
            params={"role": "outbound", "status": "QUEUED"},
            headers=headers)
        assert queued.status_code == 200
        assert all(row["status"] == "QUEUED" for row in queued.json())
        assert (await client.get(
            "/api/v1/requests", params={"role": "sideways"},
            headers=headers)).status_code == 422
        assert (await client.get(
            "/api/v1/requests", params={"limit": 500},
            headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_completed_request_carries_answer(db):
    from backend.identity.signing import sign_message
    from backend.protocol.models import NexusMessage, RemoteResponsePayload
    from backend.protocol.serialization import message_to_dict
    from backend.protocol.types import PROTOCOL_VERSION, MessageType

    keys = Keys()
    app, _ = _app(db, settings := make_settings(), keys)
    local, remote = await seed_peer_pair(db, keys)
    # True originator flow: create outbound, hand off (SENT), then
    # correlate the remote's signed answer (exercises the SENT gate).
    from backend.requests.service import RequestService

    service = RequestService(db.sessions, keys)
    created0, expires0 = live_times()
    record = await service.create_remote_request(
        local.agent_id, remote.agent_id, "What is two plus two?", [],
        expires0, now=created0,
    )
    sent = await service.mark_sent(record.request_id)
    assert sent.status == "SENT"
    request_id = record.request_id
    created, expires = live_times()
    from .conftest import unique as _unique

    answer_id = _unique("msg")
    answer = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=answer_id,
        request_id=request_id,
        sender=remote.agent_id,
        recipient=local.agent_id,
        type=MessageType.REMOTE_RESPONSE,
        created_at=created,
        expires_at=expires,
        payload=RemoteResponsePayload(status="ok", answer="four"),
        signature="",
    )
    envelope = message_to_dict(sign_message(answer, remote.private_key))
    done = await service.handle_remote_response(
        envelope, local.agent_id, now=created)
    assert done.status == "COMPLETED"
    async with make_client(app) as client:
        fetched = await client.get(
            f"/api/v1/requests/{request_id}",
            headers=auth_headers())
    assert fetched.status_code == 200
    assert fetched.json()["answer"] == "four"
    assert fetched.json()["response_message_id"] == answer_id
