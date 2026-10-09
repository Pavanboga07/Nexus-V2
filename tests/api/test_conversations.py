"""Conversation endpoint tests: ownership, turns, validation (real PG)."""

import uuid

import pytest

from backend.api.app import create_app

from .conftest import (  # noqa: F401
    Keys,
    auth_headers,
    db,
    make_client,
    make_settings,
    make_state,
    seed_owned_agent,
)


def _app(db, settings=None, keys=None):
    settings = settings or make_settings()
    state, keys = make_state(db, settings, keys=keys)
    return create_app(settings, state=state), keys


@pytest.mark.asyncio
async def test_conversation_lifecycle(db):
    keys = Keys()
    app, _ = _app(db, settings := make_settings(), keys)
    agent = await seed_owned_agent(db, keys)
    async with make_client(app) as client:
        headers = auth_headers()
        created = await client.post(
            "/api/v1/conversations", headers=headers,
            json={"agent_id": agent.agent_id, "title": "hello"})
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]
        listed = await client.get(
            "/api/v1/conversations",
            params={"agent_id": agent.agent_id}, headers=headers)
        assert listed.status_code == 200
        assert {row["id"] for row in listed.json()} == {conversation_id}
        turn = await client.post(
            f"/api/v1/conversations/{conversation_id}/turns",
            headers=headers, json={"message": "What is two plus two?"})
        assert turn.status_code == 200, turn.text
        assert turn.json()["answer"] == "api answer"
        assert turn.json()["conversation_id"] == conversation_id
        messages = await client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            params={"agent_id": agent.agent_id}, headers=headers)
        assert messages.status_code == 200
        roles = [row["role"] for row in messages.json()]
        assert roles == ["user", "agent"]
        assert messages.json()[0]["content"] == "What is two plus two?"


@pytest.mark.asyncio
async def test_conversation_other_owner_is_404(db):
    import uuid as uuid_mod

    from backend.persistence.database import session_scope
    from backend.persistence.repositories.conversations import (
        ConversationRepository,
    )

    keys = Keys()
    app, _ = _app(db, settings := make_settings(), keys)
    other_owner = uuid_mod.uuid4()
    stranger = await seed_owned_agent(db, keys, owner=other_owner)
    async with session_scope(db.sessions) as session:
        row = await ConversationRepository(session).create(
            stranger.agent_id, title="theirs")
        foreign_id = str(row.id)
    async with make_client(app) as client:
        headers = auth_headers()
        # Another user's conversation: invisible, not forbidden.
        messages = await client.get(
            f"/api/v1/conversations/{foreign_id}/messages",
            headers=headers)
        assert messages.status_code == 404
        turn = await client.post(
            f"/api/v1/conversations/{foreign_id}/turns",
            headers=headers, json={"message": "hi"})
        assert turn.status_code == 404
        listed = await client.get(
            "/api/v1/conversations",
            params={"agent_id": stranger.agent_id}, headers=headers)
        assert listed.status_code == 404


@pytest.mark.asyncio
async def test_conversation_validation(db):
    keys = Keys()
    app, _ = _app(db, settings := make_settings(), keys)
    agent = await seed_owned_agent(db, keys)
    async with make_client(app) as client:
        headers = auth_headers()
        assert (await client.post(
            "/api/v1/conversations", headers=headers,
            json={"agent_id": agent.agent_id, "title": ""})).status_code == 422
        created = await client.post(
            "/api/v1/conversations", headers=headers,
            json={"agent_id": agent.agent_id})
        conversation_id = created.json()["id"]
        assert (await client.post(
            f"/api/v1/conversations/{conversation_id}/turns",
            headers=headers,
            json={"message": ""})).status_code == 422
        assert (await client.post(
            f"/api/v1/conversations/{conversation_id}/turns",
            headers=headers,
            json={"message": "x" * 65537})).status_code == 422
        assert (await client.get(
            "/api/v1/conversations/not-a-uuid/messages",
            params={"agent_id": agent.agent_id},
            headers=headers)).status_code == 422
        assert (await client.get(
            "/api/v1/conversations",
            params={"agent_id": agent.agent_id, "limit": 500},
            headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_conversations_need_model(db):
    from backend.api.app import ApiState

    keys = Keys()
    settings = make_settings()
    state = ApiState(settings=settings, sessions=db.sessions,
                     engine=db.engine)
    app = create_app(settings, state=state)
    agent = await seed_owned_agent(db, keys)
    async with make_client(app) as client:
        headers = auth_headers()
        response = await client.post(
            "/api/v1/conversations", headers=headers,
            json={"agent_id": agent.agent_id})
        assert response.status_code == 503
        assert response.json()["code"] == "MODEL_NOT_CONFIGURED"
