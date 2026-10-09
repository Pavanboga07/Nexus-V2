"""Shared fixtures for Component 10 tests (isolated real PostgreSQL).

Uses a dedicated database so outbox-worker tests can claim PENDING
rows without touching other suites' mail.
"""

from __future__ import annotations

import asyncio
import base64
import os
import pathlib
import types
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool

from backend.agent.config import AgentRuntimeConfig
from backend.agent.runtime import AgentRuntime
from backend.execution import RemoteExecutor
from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.identity.signing import sign_message
from backend.inbound import InboundProcessor
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.requests.message_builder import build_remote_query_message
from backend.requests.service import RequestService

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_execution",
)


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


def _pub(identity):
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unique(prefix):
    return f"{prefix}10_{uuid.uuid4().hex}"


def live_times():
    created = datetime.now(timezone.utc)
    return created, created + timedelta(hours=1)


class Keys:
    """Identity provider stub holding private keys."""

    def __init__(self):
        self._keys = {}

    def add(self, identity):
        self._keys[identity.agent_id] = identity.private_key
        return identity

    def private_key_for(self, agent_id):
        return self._keys[agent_id]


def signed_query(sender, recipient, query, request_id, message_id,
                 created, expires):
    unsigned = build_remote_query_message(
        sender_agent_id=sender.agent_id,
        recipient_agent_id=recipient.agent_id,
        request_id=request_id,
        message_id=message_id,
        query=query,
        requested_capabilities=["calendar.read"],
        created_at=created,
        expires_at=expires,
    )
    return sign_message(unsigned, sender.private_key)


async def seed_approved(db, keys, query="What is two plus two?"):
    """Register an inbound request through Component 9, then approve it.

    Returns (sender, recipient, request_id) with the row APPROVED.
    """
    sender = keys.add(create_identity())
    recipient = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(sender.agent_id, _pub(sender))
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient)
        )
        await PeerRepository(session).create(
            recipient.agent_id, sender.agent_id, status="connected"
        )
    created, expires = live_times()
    message = signed_query(
        sender, recipient, query, unique("req"), unique("msg"),
        created, expires,
    )
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    outcome = await processor.process(message)
    assert outcome.status == "REGISTERED"
    service = RequestService(db.sessions, keys)
    approved = await service.approve(
        message.request_id, recipient.agent_id
    )
    assert approved.status == "APPROVED"
    return sender, recipient, message.request_id


class AnswerModel(BaseChatModel):
    """Deterministic model answering with a fixed text."""

    text: str = "four"
    calls: int = 0
    seen_inputs: Any = None

    @property
    def _llm_type(self) -> str:
        return "answer-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.seen_inputs is not None:
            self.seen_inputs.append(
                [m.content for m in messages
                 if isinstance(m, HumanMessage)]
            )
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content=self.text))])


class EchoModel(BaseChatModel):
    """Returns the last human message verbatim (isolation probe)."""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "echo-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        human = [m for m in messages if isinstance(m, HumanMessage)]
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content=human[-1].content))])


class ScriptedModel(BaseChatModel):
    """Pops scripted steps (messages or exceptions)."""

    steps: Any = None
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])


def make_runtime(model, tools=None):
    return AgentRuntime(AgentRuntimeConfig(), model=model, tools=tools)


def make_executor(db, keys, recipient_id, runtime):
    return RemoteExecutor(db.sessions, recipient_id, keys, runtime)


def local_calc_tool(tool_calls_log):
    """A real LangChain tool executed through the graph's ToolNode."""
    from langchain_core.tools import StructuredTool

    def calc_add(a: int, b: int) -> int:
        """Add two numbers."""
        tool_calls_log.append((a, b))
        return a + b

    return StructuredTool.from_function(
        func=calc_add, name="calc_add", description="Add two numbers.")


async def response_messages(db, request_id):
    """Stored REMOTE_RESPONSE rows for a request (not the inbound query)."""
    from backend.persistence.database import session_scope
    from backend.persistence.repositories.messages import MessageRepository

    async with session_scope(db.sessions) as session:
        rows = await MessageRepository(session).list_by_request(request_id)
        return [row for row in rows
                if row.message_type == "REMOTE_RESPONSE"]
