"""Approval and execution endpoint tests: authorization first (real PG).

Approvals and execution act as the persisted recipient — never a
caller-supplied identity. Execution runs the real Component 10
executor with a deterministic model; unapproved work is refused
before any model invocation.
"""

import pytest

from backend.api.app import create_app
from backend.requests.service import RequestService

from .conftest import (  # noqa: F401
    Keys,
    auth_headers,
    db,
    make_client,
    make_settings,
    make_state,
    seed_inbound_request,
    seed_peer_pair,
)


class _ReadKeys:
    def private_key_for(self, agent_id):
        raise KeyError(agent_id)


def _app(db, settings=None, keys=None):
    settings = settings or make_settings()
    state, keys = make_state(db, settings, keys=keys)
    return create_app(settings, state=state), keys, state


async def _status(db, request_id):
    service = RequestService(db.sessions, _ReadKeys())
    record = await service.get_request(request_id)
    return record.status if record else None


@pytest.mark.asyncio
async def test_pending_lists_inbound_awaiting(db):
    keys = Keys()
    app, _, _ = _app(db, settings := make_settings(), keys)
    local, _, request_id = await seed_inbound_request(db, keys)
    async with make_client(app) as client:
        pending = await client.get(
            "/api/v1/approvals/pending", headers=auth_headers())
    assert pending.status_code == 200
    assert request_id in {row["request_id"] for row in pending.json()}


@pytest.mark.asyncio
async def test_approve_and_reject_flow(db):
    keys = Keys()
    app, _, _ = _app(db, settings := make_settings(), keys)
    _, _, first = await seed_inbound_request(db, keys)
    _, _, second = await seed_inbound_request(db, keys)
    async with make_client(app) as client:
        headers = auth_headers()
        approved = await client.post(
            f"/api/v1/requests/{first}/approve", headers=headers,
            json={"reason": "looks fine"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"
        assert await _status(db, first) == "APPROVED"
        again = await client.post(
            f"/api/v1/requests/{first}/approve", headers=headers,
            json={})
        assert again.status_code == 409
        rejected = await client.post(
            f"/api/v1/requests/{second}/reject", headers=headers,
            json={"reason": "nope"})
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "REJECTED"
        pending = await client.get(
            "/api/v1/approvals/pending", headers=headers)
        ids = {row["request_id"] for row in pending.json()}
        assert first not in ids and second not in ids


@pytest.mark.asyncio
async def test_approve_outbound_is_forbidden(db):
    keys = Keys()
    app, _, _ = _app(db, settings := make_settings(), keys)
    local, remote = await seed_peer_pair(db, keys)
    from .conftest import live_times

    service = RequestService(db.sessions, keys)
    created, expires = live_times()
    record = await service.create_remote_request(
        local.agent_id, remote.agent_id, "mine?", [], expires, now=created)
    async with make_client(app) as client:
        # The caller owns the sender side, not the recipient side.
        response = await client.post(
            f"/api/v1/requests/{record.request_id}/approve",
            headers=auth_headers(), json={})
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_execute_requires_approval(db):
    from .conftest import AnswerModel

    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    model = state.runtime._model
    app = create_app(settings, state=state)
    _, _, request_id = await seed_inbound_request(db, keys)
    async with make_client(app) as client:
        before = await client.post(
            f"/api/v1/requests/{request_id}/execute",
            headers=auth_headers())
        assert before.status_code == 409
        assert model.calls == 0
        service = RequestService(db.sessions, keys)
        local_id = (await service.get_request(request_id)
                    ).recipient_agent_id
        await service.approve(request_id, local_id)
        done = await client.post(
            f"/api/v1/requests/{request_id}/execute",
            headers=auth_headers())
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "COMPLETED"
        assert done.json()["executed"] is True
        assert model.calls == 1
        assert await _status(db, request_id) == "COMPLETED"
        # A completed request cannot be executed again.
        repeat = await client.post(
            f"/api/v1/requests/{request_id}/execute",
            headers=auth_headers())
        assert repeat.status_code == 409
        assert model.calls == 1


@pytest.mark.asyncio
async def test_execute_without_persisted_approval_row(db):
    from backend.persistence.database import session_scope
    from backend.persistence.repositories.remote_requests import (
        RemoteRequestRepository,
    )

    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    app = create_app(settings, state=state)
    _, _, _ = await seed_inbound_request(db, keys)
    # Hand-built APPROVED row with no approval decision recorded.
    from backend.identity.identity import create_identity

    from .conftest import _pub, live_times, unique

    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        from backend.persistence.repositories.agents import AgentRepository
        from backend.persistence.repositories.peers import PeerRepository
        from .conftest import OWNER

        await AgentRepository(session).create(
            sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient), owner_user_id=OWNER)
        await PeerRepository(session).create(
            recipient.agent_id, sender.agent_id, status="connected")
        await PeerRepository(session).create(
            sender.agent_id, recipient.agent_id, status="connected")
    created, expires = live_times()
    from backend.requests.service import RequestService as Service

    service = Service(db.sessions, keys)
    record = await service.create_remote_request(
        sender.agent_id, recipient.agent_id, "Sneaky?", ["calendar.read"],
        expires, now=created,
    )
    async with session_scope(db.sessions) as session:
        await RemoteRequestRepository(session).update(
            record.request_id, status="APPROVED")
    async with make_client(app) as client:
        response = await client.post(
            f"/api/v1/requests/{record.request_id}/execute",
            headers=auth_headers())
    # Owned recipient side, APPROVED status, but no approval decision
    # row: the executor refuses before any model invocation.
    assert response.status_code == 409
    assert response.json()["code"] == "APPROVAL_MISSING"
    assert state.runtime._model.calls == 0


@pytest.mark.asyncio
async def test_execute_stuck_processing_replays(db):
    keys = Keys()
    settings = make_settings()
    state, _ = make_state(db, settings, keys)
    app = create_app(settings, state=state)
    local, _, request_id = await seed_inbound_request(db, keys)
    service = RequestService(db.sessions, keys)
    await service.approve(request_id, local.agent_id)
    await service.mark_processing(request_id)
    async with make_client(app) as client:
        response = await client.post(
            f"/api/v1/requests/{request_id}/execute",
            headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["status"] == "IN_PROGRESS"
    assert response.json()["executed"] is False


@pytest.mark.asyncio
async def test_execution_needs_runtime_and_identities(db):
    from backend.api.app import ApiState

    keys = Keys()
    _, _, request_id = await seed_inbound_request(db, keys)
    service = RequestService(db.sessions, keys)
    record = await service.get_request(request_id)
    await service.approve(request_id, record.recipient_agent_id)
    settings = make_settings()
    state = ApiState(settings=settings, sessions=db.sessions,
                     engine=db.engine)
    app = create_app(settings, state=state)
    async with make_client(app) as client:
        response = await client.post(
            f"/api/v1/requests/{request_id}/execute",
            headers=auth_headers())
    assert response.status_code == 503
    assert response.json()["code"] == "EXECUTION_NOT_CONFIGURED"
