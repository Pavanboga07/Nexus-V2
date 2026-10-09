"""Agent and peer endpoint tests: ownership boundaries (real PG)."""

import uuid

import pytest

from backend.api.app import create_app

from .conftest import (  # noqa: F401
    OWNER,
    Keys,
    auth_headers,
    db,
    make_client,
    make_settings,
    make_state,
    seed_owned_agent,
    seed_peer_pair,
)


def _app(db, settings=None, keys=None):
    settings = settings or make_settings()
    state, keys = make_state(db, settings, keys=keys)
    return create_app(settings, state=state), keys


@pytest.mark.asyncio
async def test_list_agents_returns_only_owned(db):
    keys = Keys()
    settings = make_settings()
    app, _ = _app(db, settings, keys)
    mine = await seed_owned_agent(db, keys)
    other_owner = uuid.uuid4()
    stranger = await seed_owned_agent(db, keys, owner=other_owner)
    async with make_client(app) as client:
        response = await client.get("/api/v1/agents",
                                    headers=auth_headers())
    assert response.status_code == 200
    ids = {row["agent_id"] for row in response.json()}
    assert mine.agent_id in ids
    assert stranger.agent_id not in ids
    mine_row = [row for row in response.json()
                if row["agent_id"] == mine.agent_id][0]
    assert mine_row["owned"] is True
    assert mine_row["active"] is True
    assert "private_key" not in response.text
    assert "signature" not in response.text


@pytest.mark.asyncio
async def test_list_peers_owned_agent(db):
    keys = Keys()
    settings = make_settings()
    app, _ = _app(db, settings, keys)
    local, remote = await seed_peer_pair(db, keys)
    async with make_client(app) as client:
        response = await client.get(
            f"/api/v1/agents/{local.agent_id}/peers",
            headers=auth_headers())
    assert response.status_code == 200
    peers = {row["peer_agent_id"]: row["status"]
             for row in response.json()}
    assert peers.get(remote.agent_id) == "connected"


@pytest.mark.asyncio
async def test_peers_unowned_agent_is_404(db):
    keys = Keys()
    settings = make_settings()
    app, _ = _app(db, settings, keys)
    stranger = await seed_owned_agent(db, keys, owner=uuid.uuid4())
    async with make_client(app) as client:
        response = await client.get(
            f"/api/v1/agents/{stranger.agent_id}/peers",
            headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "UNKNOWN_AGENT"


@pytest.mark.asyncio
async def test_peers_unknown_agent_is_404(db):
    keys = Keys()
    settings = make_settings()
    app, _ = _app(db, settings, keys)
    async with make_client(app) as client:
        response = await client.get(
            "/api/v1/agents/agent_doesnotexist/peers",
            headers=auth_headers())
    assert response.status_code == 404
