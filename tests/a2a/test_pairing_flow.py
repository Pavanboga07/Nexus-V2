"""Pairing full cycle: invite → accept → approve → complete → task.

Two isolated Nexus instances (separate databases, owners, keys) plus
one real GatewayServer, driven over HTTP with real worker loops. The
test proves the complete trust lifecycle and then a real remote task
through the newly paired peers:

B issues invitation → A validates + accepts → A's worker sends →
gateway routes → B registers via invitation bootstrap (invitation
burned, pending peer) → B approves → B completes (signed
APPROVAL_RESPONSE over the live gateway) → A's handler completes the
link and connects the peer → A submits a task → full task lifecycle
→ verified answer on A.

Only the model is deterministic (at the model boundary); transport,
signing, approval, execution, reception, and persistence are real.
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
    "NEXUS_TEST_A2A_PAIR_URL_A",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_a2a_pair_a",
)
TEST_URL_B = os.environ.get(
    "NEXUS_TEST_A2A_PAIR_URL_B",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_a2a_pair_b",
)
QUERY = "Summarize the paired demo notes."
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
async def test_pairing_then_task_end_to_end(db):
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    token_a = f"pair-token-a-{uuid.uuid4().hex}"
    token_b = f"pair-token-b-{uuid.uuid4().hex}"
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

        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}
        async with _client(apps["a"][0]) as client_a, _client(
            apps["b"][0]
        ) as client_b:
            # B issues an invitation for B's agent.
            issued = await client_b.post(
                "/api/v1/pairings/invitations", headers=headers_b,
                json={"agent_id": agent_b.agent_id, "ttl_seconds": 3600},
            )
            assert issued.status_code == 201, issued.text
            invitation = issued.json()["invitation"]

            # A validates (crypto + expiry, no state change)...
            validated = await client_a.post(
                "/api/v1/pairings/validate", headers=headers_a,
                json={"invitation": invitation})
            assert validated.status_code == 200, validated.text
            assert validated.json()["agent_id"] == agent_b.agent_id

            # ...then accepts: pairing request dispatched via gateway.
            accepted = await client_a.post(
                "/api/v1/pairings/accept", headers=headers_a,
                json={"invitation": invitation,
                      "sender_agent_id": agent_a.agent_id})
            assert accepted.status_code == 201, accepted.text
            pairing_id = accepted.json()["pairing_id"]
            pair_request_id = accepted.json()["request_id"]

            # B's real inbound path registers it (invitation burned).
            async def _pending_pair():
                response = await client_b.get(
                    "/api/v1/approvals/pending", headers=headers_b)
                assert response.status_code == 200, response.text
                return next(
                    (item for item in response.json()
                     if item["request_id"] == pair_request_id),
                    None,
                )

            found = await _wait_for(
                "B's pairing approval", 60.0, _pending_pair)
            assert found["status"] == "WAITING_APPROVAL"
            assert "nexus.pair" in found["requested_capabilities"]

            # B approves through the standard endpoint...
            approved = await client_b.post(
                f"/api/v1/requests/{pair_request_id}/approve",
                headers=headers_b, json={})
            assert approved.status_code == 200, approved.text

            # ...then completes: peer connected, signed acceptance sent.
            done = await client_b.post(
                "/api/v1/pairings/complete", headers=headers_b,
                json={"request_id": pair_request_id})
            assert done.status_code == 200, done.text
            assert done.json()["approved"] is True
            assert done.json()["ack"] in ("DELIVERED", "BUFFERED")

            # A's handler completes the link and connects the peer.
            async def _link_done():
                response = await client_a.get(
                    "/api/v1/pairings", headers=headers_a,
                    params={"agent_id": agent_a.agent_id})
                assert response.status_code == 200, response.text
                return next(
                    (item for item in response.json()
                     if item["pairing_id"] == pairing_id
                     and item["status"] == "completed"),
                    None,
                )

            assert await _wait_for(
                "A's completed link", 60.0, _link_done) is not None

            # Both peers are connected (B's row flipped at complete,
            # A's at acceptance).
            async with session_scope(db.b.sessions) as session:
                peer_b = await PeerRepository(session).get(
                    agent_b.agent_id, agent_a.agent_id)
                assert peer_b is not None
                assert peer_b.status == "connected"
            async with session_scope(db.a.sessions) as session:
                peer_a = await PeerRepository(session).get(
                    agent_a.agent_id, agent_b.agent_id)
                assert peer_a is not None
                assert peer_a.status == "connected"

            # Now a REAL task flows through the paired trust.
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
            task_id = created.json()["request_id"]

            async def _task_pending():
                response = await client_b.get(
                    "/api/v1/approvals/pending", headers=headers_b)
                assert response.status_code == 200, response.text
                return next(
                    (item for item in response.json()
                     if item["request_id"] == task_id),
                    None,
                )

            assert await _wait_for(
                "B's task approval", 60.0, _task_pending) is not None
            approved = await client_b.post(
                f"/api/v1/requests/{task_id}/approve",
                headers=headers_b, json={})
            assert approved.status_code == 200, approved.text
            executed = await client_b.post(
                f"/api/v1/requests/{task_id}/execute",
                headers=headers_b)
            assert executed.status_code == 200, executed.text
            assert executed.json()["status"] == "COMPLETED"
            response_message_id = executed.json()["response_message_id"]
            assert response_message_id

            async def _task_done():
                response = await client_a.get(
                    f"/api/v1/requests/{task_id}", headers=headers_a)
                assert response.status_code == 200, response.text
                body = response.json()
                return body if body["status"] == "COMPLETED" else None

            final = await _wait_for(
                "A's completed task", 60.0, _task_done)
            assert final["response_message_id"] == response_message_id
            assert final["answer"] == EXPECTED_ANSWER
    finally:
        for state in states:
            await close_state(state)
        await server.stop()
