"""Pairing routes: invitations, acceptance, completion, revocation.

Single database hosting two owners (owner scoping is per request).
B-side completion uses a stub GatewayClient for the transport step;
the live transport path is covered by tests/a2a/test_pairing_flow.py.
"""

import pytest

from backend.gateway_client.models import AckResult
from backend.identity.identity import create_identity
from backend.identity.verification import verify_message
from backend.protocol.serialization import message_from_dict

from .conftest import (  # noqa: F401
    Keys,
    db,
    ensure_user,
    make_client,
    make_settings,
    make_state,
)


class _StubClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, envelope):
        self.sent.append(envelope)
        return AckResult(
            message_id=envelope["message_id"], status="DELIVERED",
            request_id=envelope["request_id"])


async def _two_apps(db, monkeypatch=None):
    from backend.api.app import create_app

    keys_a, keys_b = Keys(), Keys()
    owner_a, owner_b = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )
    import uuid

    owner_a, owner_b = uuid.UUID(owner_a), uuid.UUID(owner_b)
    await ensure_user(db, owner_a)
    await ensure_user(db, owner_b)
    agent_a = keys_a.add(create_identity())
    agent_b = keys_b.add(create_identity())
    settings_a = make_settings(
        api_token="token-a", owner_user_id=owner_a)
    settings_b = make_settings(
        api_token="token-b", owner_user_id=owner_b)
    state_a, _ = make_state(db, settings_a, keys_a)
    state_b, _ = make_state(db, settings_b, keys_b)
    from backend.persistence.database import session_scope
    from backend.persistence.repositories.agents import AgentRepository
    from backend.persistence.repositories.peers import PeerRepository
    from tests.api.conftest import _pub

    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(
            agent_a.agent_id, _pub(agent_a), owner_user_id=owner_a)
        await AgentRepository(session).create(
            agent_b.agent_id, _pub(agent_b), owner_user_id=owner_b)
    app_a = create_app(settings_a, state=state_a)
    app_b = create_app(settings_b, state=state_b)
    stub = _StubClient()
    state_b.gateway_clients[agent_b.agent_id] = stub
    return {
        "agent_a": agent_a, "agent_b": agent_b,
        "app_a": app_a, "app_b": app_b,
        "state_a": state_a, "state_b": state_b,
        "stub": stub,
    }


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_invitation_lifecycle(db):
    ctx = await _two_apps(db)
    async with make_client(ctx["app_b"]) as client:
        created = await client.post(
            "/api/v1/pairings/invitations", headers=_auth("token-b"),
            json={"agent_id": ctx["agent_b"].agent_id,
                  "ttl_seconds": 3600})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["invitation"].startswith("inv.")
        assert body["secret"]
        invitation_id = body["invitation_id"]

        listed = await client.get(
            "/api/v1/pairings/invitations",
            headers=_auth("token-b"),
            params={"agent_id": ctx["agent_b"].agent_id})
        assert listed.status_code == 200, listed.text
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["used"] is False
        assert rows[0]["revoked"] is False

        validated = await client.post(
            "/api/v1/pairings/validate", headers=_auth("token-b"),
            json={"invitation": body["invitation"]})
        assert validated.status_code == 200, validated.text
        info = validated.json()
        assert info["agent_id"] == ctx["agent_b"].agent_id
        assert len(info["fingerprint"]) == 19

        revoked = await client.post(
            f"/api/v1/pairings/invitations/{invitation_id}/revoke",
            headers=_auth("token-b"))
        assert revoked.status_code == 200, revoked.text
        assert revoked.json() == {"revoked": True}

        listed = await client.get(
            "/api/v1/pairings/invitations",
            headers=_auth("token-b"),
            params={"agent_id": ctx["agent_b"].agent_id})
        assert listed.json()[0]["revoked"] is True


@pytest.mark.asyncio
async def test_invitation_issuer_must_be_owned(db):
    ctx = await _two_apps(db)
    async with make_client(ctx["app_a"]) as client:
        # A cannot issue for B's agent: unknown-or-unowned is 404.
        response = await client.post(
            "/api/v1/pairings/invitations", headers=_auth("token-a"),
            json={"agent_id": ctx["agent_b"].agent_id})
        assert response.status_code == 404, response.text
        # Unknown invitation revocation is 404, not 500.
        missing = await client.post(
            "/api/v1/pairings/invitations/inv-missing/revoke",
            headers=_auth("token-a"))
        assert missing.status_code == 404, missing.text


@pytest.mark.asyncio
async def test_accept_creates_pairing_request(db):
    ctx = await _two_apps(db)
    async with make_client(ctx["app_b"]) as client_b:
        created = await client_b.post(
            "/api/v1/pairings/invitations", headers=_auth("token-b"),
            json={"agent_id": ctx["agent_b"].agent_id})
        invitation = created.json()["invitation"]
        own = await client_b.post(
            "/api/v1/pairings/invitations", headers=_auth("token-b"),
            json={"agent_id": ctx["agent_b"].agent_id})
        own_invitation = own.json()["invitation"]
    async with make_client(ctx["app_a"]) as client_a:
        accepted = await client_a.post(
            "/api/v1/pairings/accept", headers=_auth("token-a"),
            json={"invitation": invitation,
                  "sender_agent_id": ctx["agent_a"].agent_id})
        assert accepted.status_code == 201, accepted.text
        body = accepted.json()
        assert body["status"] == "pending"
        request_id = body["request_id"]
        assert request_id.startswith("req_")

        # Double accept is idempotent (same link, no duplicate).
        again = await client_a.post(
            "/api/v1/pairings/accept", headers=_auth("token-a"),
            json={"invitation": invitation,
                  "sender_agent_id": ctx["agent_a"].agent_id})
        assert again.status_code == 201, again.text
        assert again.json()["request_id"] == request_id

        # The pairing request is a real request row.
        fetched = await client_a.get(
            f"/api/v1/requests/{request_id}", headers=_auth("token-a"))
        assert fetched.status_code == 200, fetched.text
        assert "nexus.pair" in fetched.json()["requested_capabilities"]
    # Self-pairing is rejected.
    async with make_client(ctx["app_b"]) as client_b:
        selfish = await client_b.post(
            "/api/v1/pairings/accept", headers=_auth("token-b"),
            json={"invitation": own_invitation,
                  "sender_agent_id": ctx["agent_b"].agent_id})
        assert selfish.status_code == 422, selfish.text


@pytest.mark.asyncio
async def test_complete_sends_signed_acceptance(db):
    ctx = await _two_apps(db)
    async with make_client(ctx["app_b"]) as client_b:
        created = await client_b.post(
            "/api/v1/pairings/invitations", headers=_auth("token-b"),
            json={"agent_id": ctx["agent_b"].agent_id})
        invitation = created.json()["invitation"]
    async with make_client(ctx["app_a"]) as client_a:
        accepted = await client_a.post(
            "/api/v1/pairings/accept", headers=_auth("token-a"),
            json={"invitation": invitation,
                  "sender_agent_id": ctx["agent_a"].agent_id})
        request_id = accepted.json()["request_id"]
    # Walk the shared row through the transport states the live
    # Gateway path would persist (route tests run no worker).
    from backend.requests.service import RequestService

    service = RequestService(db.sessions, ctx["state_b"].identities)
    await service.mark_sent(request_id)
    await service.mark_delivered(request_id)
    await service.mark_waiting_approval(request_id)
    # B approves through the standard endpoint, then completes.
    async with make_client(ctx["app_b"]) as client_b:
        approved = await client_b.post(
            f"/api/v1/requests/{request_id}/approve",
            headers=_auth("token-b"), json={})
        assert approved.status_code == 200, approved.text
        done = await client_b.post(
            "/api/v1/pairings/complete", headers=_auth("token-b"),
            json={"request_id": request_id})
        assert done.status_code == 200, done.text
        body = done.json()
        assert body["approved"] is True
        assert body["ack"] == "DELIVERED"

    assert len(ctx["stub"].sent) == 1
    envelope = ctx["stub"].sent[0]
    assert envelope["request_id"] == request_id
    assert envelope["sender"] == ctx["agent_b"].agent_id
    assert envelope["recipient"] == ctx["agent_a"].agent_id
    # The acceptance signature verifies against B's real key.
    from backend.identity.keys import public_key_to_bytes

    message = message_from_dict(envelope)
    assert verify_message(
        message, ctx["agent_b"].public_key) is not None

    # B's peer row is connected; retrying completion re-sends safely.
    from backend.persistence.database import session_scope
    from backend.persistence.repositories.peers import PeerRepository

    async with session_scope(db.sessions) as session:
        peer = await PeerRepository(session).get(
            ctx["agent_b"].agent_id, ctx["agent_a"].agent_id)
        assert peer is not None and peer.status == "connected"
    async with make_client(ctx["app_b"]) as client_b:
        again = await client_b.post(
            "/api/v1/pairings/complete", headers=_auth("token-b"),
            json={"request_id": request_id})
        assert again.status_code == 200, again.text
    assert len(ctx["stub"].sent) == 2


@pytest.mark.asyncio
async def test_complete_requires_decision(db):
    ctx = await _two_apps(db)
    async with make_client(ctx["app_b"]) as client_b:
        created = await client_b.post(
            "/api/v1/pairings/invitations", headers=_auth("token-b"),
            json={"agent_id": ctx["agent_b"].agent_id})
        invitation = created.json()["invitation"]
    async with make_client(ctx["app_a"]) as client_a:
        accepted = await client_a.post(
            "/api/v1/pairings/accept", headers=_auth("token-a"),
            json={"invitation": invitation,
                  "sender_agent_id": ctx["agent_a"].agent_id})
        request_id = accepted.json()["request_id"]
    # Still WAITING_APPROVAL: completion is a conflict, not a bypass.
    async with make_client(ctx["app_b"]) as client_b:
        early = await client_b.post(
            "/api/v1/pairings/complete", headers=_auth("token-b"),
            json={"request_id": request_id})
        assert early.status_code == 409, early.text
        assert early.json()["code"] == "PAIRING_CONFLICT"
        assert ctx["stub"].sent == []


@pytest.mark.asyncio
async def test_peer_revoke_invalidates_authorization(db):
    ctx = await _two_apps(db)
    from backend.persistence.database import session_scope
    from backend.persistence.repositories.peers import PeerRepository

    async with session_scope(db.sessions) as session:
        await PeerRepository(session).create(
            ctx["agent_a"].agent_id, ctx["agent_b"].agent_id,
            status="connected")
    async with make_client(ctx["app_a"]) as client:
        revoked = await client.post(
            f"/api/v1/peers/{ctx['agent_b'].agent_id}/revoke",
            headers=_auth("token-a"))
        assert revoked.status_code == 200, revoked.text
        assert revoked.json() == {"revoked": 1}
        again = await client.post(
            f"/api/v1/peers/{ctx['agent_b'].agent_id}/revoke",
            headers=_auth("token-a"))
        assert again.json() == {"revoked": 0}
    async with session_scope(db.sessions) as session:
        peer = await PeerRepository(session).get(
            ctx["agent_a"].agent_id, ctx["agent_b"].agent_id)
        assert peer.status == "revoked"
