"""Local A2A demo: two isolated Nexus instances, one real GatewayServer.

User A submits a remote task to User B's agent. The task travels A's
outbox worker → real GatewayServer → B's dispatcher → B's real inbound
processor (persisted WAITING_APPROVAL) → explicit approval over B's HTTP
API → real execution through B's agent runtime (deterministic
development model at the model boundary only) → signed response
persisted + queued by B → B's worker → gateway → A's real response
receiver → A's persisted COMPLETED with the verified answer.

Nothing on the wire or lifecycle is mocked: real sockets, real
signatures, real PostgreSQL, real HTTP routes, real worker loops. Only
the model itself is deterministic (no paid credentials needed).
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
    "NEXUS_TEST_A2A_URL_A",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_a2a_a",
)
TEST_URL_B = os.environ.get(
    "NEXUS_TEST_A2A_URL_B",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_a2a_b",
)
QUERY = "Summarize the demo release notes."
EXPECTED_ANSWER = f"[deterministic:a2a-demo] {QUERY}"


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
    """Minimal identity provider backed by test identities."""

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
    last = None
    while True:
        last = await probe()
        if last:
            return last
        if loop.time() >= deadline:
            raise AssertionError(
                f"timed out waiting for {description}: last={last!r}")
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_two_instance_a2a_demo_end_to_end(db):
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    token_a = f"a2a-token-a-{uuid.uuid4().hex}"
    token_b = f"a2a-token-b-{uuid.uuid4().hex}"
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
                model="deterministic:a2a-demo",
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
        state_a, state_b = states

        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}
        async with _client(apps["a"][0]) as client_a, _client(
            apps["b"][0]
        ) as client_b:
            # 1. A submits a remote task to B's agent over A's HTTP API.
            created = await client_a.post(
                "/api/v1/requests",
                headers=headers_a,
                json={
                    "sender_agent_id": agent_a.agent_id,
                    "recipient_agent_id": agent_b.agent_id,
                    "query": QUERY,
                },
            )
            assert created.status_code == 201, created.text
            request_id = created.json()["request_id"]
            assert created.json()["status"] == "QUEUED"

            # 2-3. A's worker sends it; the gateway routes it; B's real
            # inbound path persists WAITING_APPROVAL for User B.
            async def _pending():
                response = await client_b.get(
                    "/api/v1/approvals/pending", headers=headers_b)
                assert response.status_code == 200, response.text
                items = response.json()
                return next(
                    (item for item in items
                     if item["request_id"] == request_id),
                    None,
                )

            found = await _wait_for(
                "B's approval inbox", 60.0, _pending)
            assert found["status"] == "WAITING_APPROVAL"
            assert found["sender_agent_id"] == agent_a.agent_id
            assert found["recipient_agent_id"] == agent_b.agent_id
            assert found["query"] == QUERY

            # 4-5. User B explicitly approves over HTTP.
            approved = await client_b.post(
                f"/api/v1/requests/{request_id}/approve",
                headers=headers_b,
                json={},
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "APPROVED"

            # 6-7. B executes through the real runtime; the signed
            # response is persisted and queued by B.
            executed = await client_b.post(
                f"/api/v1/requests/{request_id}/execute",
                headers=headers_b,
            )
            assert executed.status_code == 200, executed.text
            assert executed.json()["status"] == "COMPLETED"
            assert executed.json()["executed"] is True
            response_message_id = executed.json()["response_message_id"]
            assert response_message_id

            # 8-9. B's worker delivers it; A's real receiver validates,
            # persists, and completes the original request.
            async def _completed():
                response = await client_a.get(
                    f"/api/v1/requests/{request_id}", headers=headers_a)
                assert response.status_code == 200, response.text
                body = response.json()
                if body["status"] != "COMPLETED":
                    return None
                return body

            final = await _wait_for(
                "A's completed request", 60.0, _completed)
            assert final["request_id"] == request_id
            assert final["sender_agent_id"] == agent_a.agent_id
            assert final["recipient_agent_id"] == agent_b.agent_id
            assert final["response_message_id"] == response_message_id
            assert final["answer"] == EXPECTED_ANSWER

            # Both sides agree on the terminal state (shared row).
            detail_b = await client_b.get(
                f"/api/v1/requests/{request_id}", headers=headers_b)
            assert detail_b.status_code == 200, detail_b.text
            assert detail_b.json()["status"] == "COMPLETED"
            assert detail_b.json()["answer"] == EXPECTED_ANSWER
    finally:
        for state in states:
            await close_state(state)
        await server.stop()

    assert state_a.gateway_clients == {}
    assert state_b.gateway_clients == {}
