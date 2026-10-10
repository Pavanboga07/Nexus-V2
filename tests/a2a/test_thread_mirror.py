"""Thread mirror: a linked chat shows the question AND the answer.

Two isolated Nexus instances (separate databases, owners, keys) plus
one real GatewayServer, deterministic model at the model boundary
only. A creates a thread, submits a task linked to it, B approves and
executes, and the test proves the sender-side thread contains both
the mirrored question (user role) and the verified answer (agent
role, linked to the request) — the chat is no longer silent about
remote work.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import types
import uuid

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool

from backend.agent.config import AgentRuntimeConfig
from backend.agent.conversation_service import ConversationService
from backend.agent.runtime import AgentRuntime
from backend.api.app import (
    ApiState,
    _open_gateway_io,
    close_state,
    create_app,
)
from backend.api.settings import ApiSettings
from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.identity.models import AgentIdentity
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.models import User
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.persistence.repositories.users import UserRepository
from gateway.app import GatewayServer

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL_A = os.environ.get(
    "NEXUS_TEST_A2A_THREAD_URL_A",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_a2a_thread_a",
)
TEST_URL_B = os.environ.get(
    "NEXUS_TEST_A2A_THREAD_URL_B",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_a2a_thread_b",
)
QUERY = "What is three plus three?"
EXPECTED_ANSWER = f"[deterministic:a2a-thread] {QUERY}"


def _migrate(url):
    asyncio.run(ensure_database_exists(url))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def db():
    _migrate(TEST_URL_A)
    _migrate(TEST_URL_B)
    engine_a = _sa_create_engine(
        TEST_URL_A, poolclass=NullPool, pool_pre_ping=True
    )
    engine_b = _sa_create_engine(
        TEST_URL_B, poolclass=NullPool, pool_pre_ping=True
    )
    try:
        yield types.SimpleNamespace(
            a=types.SimpleNamespace(
                engine=engine_a,
                sessions=create_session_factory(engine_a),
            ),
            b=types.SimpleNamespace(
                engine=engine_b,
                sessions=create_session_factory(engine_b),
            ),
        )
    finally:
        asyncio.run(dispose_engine(engine_a))
        asyncio.run(dispose_engine(engine_b))


def _pub(identity):
    import base64

    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class _Provider:
    def __init__(self, identities):
        self._by_id = {
            identity.agent_id: identity.private_key
            for identity in identities
        }

    @property
    def agent_ids(self):
        return sorted(self._by_id)

    def identity_for(self, agent_id):
        return AgentIdentity(
            agent_id=agent_id, private_key=self._by_id[agent_id])

    def private_key_for(self, agent_id):
        return self._by_id[agent_id]


async def _seed_side(sessions, owner, local, remote):
    async with session_scope(sessions) as session:
        if await UserRepository(session).get(owner) is None:
            session.add(User(id=owner))
        agents = AgentRepository(session)
        if await agents.get(local.agent_id) is None:
            await agents.create(
                local.agent_id, _pub(local), owner_user_id=owner)
        if await agents.get(remote.agent_id) is None:
            await agents.create(remote.agent_id, _pub(remote))
        peers = PeerRepository(session)
        for left, right in (
            (local.agent_id, remote.agent_id),
            (remote.agent_id, local.agent_id),
        ):
            if await peers.get(left, right) is None:
                await peers.create(left, right, status="connected")


def _client(app):
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test")


async def _wait_for(description, timeout, probe, interval=0.25):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = await probe()
        if result:
            return result
        if loop.time() >= deadline:
            raise AssertionError(
                f"timed out waiting for {description}")
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_thread_shows_question_and_answer(db):
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    token_a = f"thread-token-a-{uuid.uuid4().hex}"
    token_b = f"thread-token-b-{uuid.uuid4().hex}"
    agent_a = create_identity()
    agent_b = create_identity()

    await _seed_side(db.a.sessions, owner_a, agent_a, agent_b)
    await _seed_side(db.b.sessions, owner_b, agent_b, agent_a)

    server = GatewayServer("127.0.0.1", 0)
    port = await server.start()
    gateway_url = f"ws://127.0.0.1:{port}"
    states = []
    try:
        apps = {}
        for tag, side, owner, token, local in (
            ("a", db.a, owner_a, token_a, agent_a),
            ("b", db.b, owner_b, token_b, agent_b),
        ):
            settings = ApiSettings(
                api_token=token,
                owner_user_id=owner,
                gateway_url=gateway_url,
            )
            runtime = AgentRuntime(AgentRuntimeConfig(
                model="deterministic:a2a-thread",
                allow_deterministic=True,
            ))
            state = ApiState(
                settings=settings,
                sessions=side.sessions,
                engine=side.engine,
                runtime=runtime,
                identities=_Provider([local]),
            )
            state.conversations = ConversationService(
                side.sessions, runtime)
            await _open_gateway_io(state, settings)
            states.append(state)
            apps[tag] = (create_app(settings, state=state), token)

        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}
        async with _client(apps["a"][0]) as client_a, _client(
            apps["b"][0]
        ) as client_b:
            made = await client_a.post(
                "/api/v1/conversations", headers=headers_a,
                json={"agent_id": agent_a.agent_id},
            )
            assert made.status_code == 201, made.text
            thread_id = made.json()["id"]

            created = await client_a.post(
                "/api/v1/requests", headers=headers_a,
                json={"sender_agent_id": agent_a.agent_id,
                      "recipient_agent_id": agent_b.agent_id,
                      "query": QUERY,
                      "conversation_id": thread_id},
            )
            assert created.status_code == 201, created.text
            assert created.json()["conversation_id"] == thread_id
            request_id = created.json()["request_id"]

            # The question mirrors into the thread immediately.
            history = await client_a.get(
                f"/api/v1/conversations/{thread_id}/messages",
                headers=headers_a)
            assert history.status_code == 200, history.text
            rows = history.json()
            assert len(rows) == 1
            assert rows[0]["role"] == "user"
            assert rows[0]["content"] == QUERY

            async def _pending():
                response = await client_b.get(
                    "/api/v1/approvals/pending", headers=headers_b)
                assert response.status_code == 200, response.text
                return next(
                    (item for item in response.json()
                     if item["request_id"] == request_id),
                    None,
                )

            assert await _wait_for(
                "B's approval", 60.0, _pending) is not None
            approved = await client_b.post(
                f"/api/v1/requests/{request_id}/approve",
                headers=headers_b, json={})
            assert approved.status_code == 200, approved.text
            executed = await client_b.post(
                f"/api/v1/requests/{request_id}/execute",
                headers=headers_b)
            assert executed.status_code == 200, executed.text
            assert executed.json()["status"] == "COMPLETED"

            # The verified answer lands in the same thread.
            async def _answered():
                response = await client_a.get(
                    f"/api/v1/conversations/{thread_id}/messages",
                    headers=headers_a)
                assert response.status_code == 200, response.text
                rows = response.json()
                answers = [row for row in rows
                           if row["role"] == "agent"]
                return answers or None

            answers = await _wait_for(
                "A's mirrored answer", 60.0, _answered)
            assert len(answers) == 1
            assert answers[0]["content"] == EXPECTED_ANSWER

            # Full transcript: question then answer, in order.
            final = await client_a.get(
                f"/api/v1/conversations/{thread_id}/messages",
                headers=headers_a)
            assert [row["role"] for row in final.json()] == [
                "user", "agent"]
    finally:
        for state in states:
            await close_state(state)
        await server.stop()
