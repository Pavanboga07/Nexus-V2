"""Shared fixtures for Component 12 API tests (isolated real PostgreSQL).

The application is built with injected state: real sessions and
services, a deterministic fake model runtime, and stub identities.
HTTP runs over httpx ASGI transport (no socket, no lifespan side
effects unless a test drives lifespan explicitly).
"""

from __future__ import annotations

import asyncio
import base64
import os
import pathlib
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool

from backend.agent.config import AgentRuntimeConfig
from backend.agent.conversation_service import ConversationService
from backend.agent.runtime import AgentRuntime
from backend.api.app import ApiState, close_state, create_app
from backend.api.settings import ApiSettings
from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.requests.service import RequestService

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_api",
)
OWNER = uuid.uuid4()
TOKEN = "test-token-" + uuid.uuid4().hex


def _migrate():
    asyncio.run(ensure_database_exists(TEST_URL))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def db():
    _migrate()
    engine = _sa_create_engine(
        TEST_URL, poolclass=NullPool, pool_pre_ping=True
    )
    try:
        yield types.SimpleNamespace(
            engine=engine, sessions=create_session_factory(engine)
        )
    finally:
        asyncio.run(dispose_engine(engine))


class Keys:
    """Identity provider stub holding private keys."""

    def __init__(self):
        self._keys = {}

    def add(self, identity):
        self._keys[identity.agent_id] = identity.private_key
        return identity

    def private_key_for(self, agent_id):
        return self._keys[agent_id]


class AnswerModel(BaseChatModel):
    """Deterministic chat model answering with fixed text."""

    text: str = "api answer"
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "answer-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content=self.text))])


def _pub(identity):
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unique(prefix):
    return f"{prefix}12_{uuid.uuid4().hex}"


def live_times():
    created = datetime.now(timezone.utc)
    return created, created + timedelta(hours=1)


def make_settings(**over):
    values = {"api_token": TOKEN, "owner_user_id": OWNER}
    values.update(over)
    return ApiSettings(**values)


def make_state(db, settings, keys=None, model=None):
    """Application state with real DB/services and injected doubles."""
    keys = keys or Keys()
    runtime = AgentRuntime(
        AgentRuntimeConfig(), model=model or AnswerModel())
    state = ApiState(
        settings=settings,
        sessions=db.sessions,
        engine=db.engine,
        runtime=runtime,
        identities=keys,
    )
    state.conversations = ConversationService(db.sessions, runtime)
    return state, keys


def make_client(app):
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test")


def auth_headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


async def ensure_user(db, user_id):
    """Create the user row if missing (agents FK to users)."""
    from backend.persistence.models import User
    from backend.persistence.repositories.users import UserRepository

    async with session_scope(db.sessions) as session:
        if await UserRepository(session).get(user_id) is None:
            session.add(User(id=user_id))


async def seed_owned_agent(db, keys, owner=OWNER, active=True):
    """Agent row owned by the caller (or another owner for negative tests)."""
    await ensure_user(db, owner)
    identity = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(
            identity.agent_id, _pub(identity), owner_user_id=owner)
        if not active:
            await AgentRepository(session).update(
                identity.agent_id, active=False)
    return identity


async def seed_peer_pair(db, keys, owner=OWNER):
    """Owned local agent plus connected remote peer, both directions."""
    local = await seed_owned_agent(db, keys, owner=owner)
    remote = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(
            remote.agent_id, _pub(remote))
        await PeerRepository(session).create(
            local.agent_id, remote.agent_id, status="connected")
    return local, remote


async def seed_inbound_request(db, keys, owner=OWNER):
    """A WAITING_APPROVAL inbound request for the caller's agent."""
    from backend.inbound import InboundProcessor
    from backend.identity.signing import sign_message
    from backend.requests.message_builder import build_remote_query_message

    local, remote = await seed_peer_pair(db, keys, owner=owner)
    created, expires = live_times()
    unsigned = build_remote_query_message(
        sender_agent_id=remote.agent_id,
        recipient_agent_id=local.agent_id,
        request_id=unique("req"),
        message_id=unique("msg"),
        query="Will you approve?",
        requested_capabilities=["calendar.read"],
        created_at=created,
        expires_at=expires,
    )
    message = sign_message(unsigned, remote.private_key)
    outcome = await InboundProcessor(
        db.sessions, local.agent_id).process(message)
    assert outcome.status == "REGISTERED"
    return local, remote, message.request_id
