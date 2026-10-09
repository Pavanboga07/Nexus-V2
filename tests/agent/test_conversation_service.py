"""Conversation service tests: isolation, atomicity, limits (real PG)."""

import asyncio
import base64
import os
import pathlib
import types
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool

from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.errors import PersistenceError
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.conversations import (
    ConversationMessageRepository,
)
from backend.persistence.repositories.users import UserRepository
from backend.agent.config import AgentRuntimeConfig
from backend.agent.conversation_service import ConversationService
from backend.agent.errors import (
    AgentNotAvailableError,
    ConversationMismatchError,
    ConversationNotFoundError,
    InvalidInputError,
    ModelInvocationError,
)
from backend.agent.runtime import AgentRuntime

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test",
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


class DeterministicChatModel(BaseChatModel):
    canned: str = "test reply"
    fail_with: Any = None
    seen: Any = None

    @property
    def _llm_type(self) -> str:
        return "deterministic-test-model"

    def _generate(self, messages: list, stop: Any = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.seen = list(messages)
        if self.fail_with is not None:
            raise self.fail_with
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.canned))])


def _service(db, **over):
    config = AgentRuntimeConfig(**over)
    runtime = AgentRuntime(config, model=DeterministicChatModel())
    return ConversationService(db.sessions, runtime)


def _pub(identity):
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


async def _owned_agent(session, active=True):
    identity = create_identity()
    owner = await UserRepository(session).create()
    await AgentRepository(session).create(
        identity.agent_id, _pub(identity), owner_user_id=owner.id)
    if not active:
        await AgentRepository(session).deactivate(identity.agent_id)
    return identity


@pytest.mark.asyncio
async def test_create_conversation_for_owned_active_agent(db):  # 15
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    assert conversation.id is not None
    assert conversation.agent_id == identity.agent_id


@pytest.mark.asyncio
async def test_submit_message_receives_reply(db):  # 16
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    turn = await service.submit_turn(
        identity.agent_id, conversation.id, "hello there")
    assert turn.response.text == "test reply"
    assert turn.conversation_id == conversation.id
    assert turn.user_message_id != turn.assistant_message_id


@pytest.mark.asyncio
async def test_both_messages_persist_after_turn(db):  # 17, 18
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    turn = await service.submit_turn(identity.agent_id, conversation.id, "hi")
    history = await service.list_history(identity.agent_id, conversation.id)
    assert [(row.role, row.content) for row in history] == [
        ("user", "hi"), ("agent", turn.response.text)]


@pytest.mark.asyncio
async def test_history_loaded_in_correct_order(db):  # 19
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    for text in ("one", "two", "three"):
        await service.submit_turn(identity.agent_id, conversation.id, text)
    history = await service.list_history(identity.agent_id, conversation.id)
    assert [row.content for row in history] == [
        "one", "test reply", "two", "test reply", "three", "test reply"]


@pytest.mark.asyncio
async def test_conversation_isolation_enforced(db):  # 20
    service = _service(db)
    async with session_scope(db.sessions) as session:
        first = await _owned_agent(session)
        second = await _owned_agent(session)
    conversation = await service.create_conversation(first.agent_id)
    with pytest.raises(ConversationMismatchError):
        await service.submit_turn(second.agent_id, conversation.id, "hi")
    with pytest.raises(ConversationMismatchError):
        await service.list_history(second.agent_id, conversation.id)


@pytest.mark.asyncio
async def test_inactive_agent_rejected(db):  # 21
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session, active=False)
    with pytest.raises(AgentNotAvailableError):
        await service.create_conversation(identity.agent_id)


@pytest.mark.asyncio
async def test_known_remote_peer_cannot_be_used(db):  # 22
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = create_identity()
        await AgentRepository(session).create(
            identity.agent_id, _pub(identity))  # NULL owner: remote peer
    with pytest.raises(AgentNotAvailableError):
        await service.create_conversation(identity.agent_id)


@pytest.mark.asyncio
async def test_missing_conversation_fails_explicitly(db):  # 23
    import uuid

    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    with pytest.raises(ConversationNotFoundError):
        await service.submit_turn(identity.agent_id, uuid.uuid4(), "hi")


@pytest.mark.asyncio
async def test_input_over_limit_rejected(db):  # 24
    service = _service(db, max_input_chars=10)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    with pytest.raises(InvalidInputError):
        await service.submit_turn(
            identity.agent_id, conversation.id, "x" * 11)
    history = await service.list_history(identity.agent_id, conversation.id)
    assert history == []  # rejected input persists nothing


@pytest.mark.asyncio
async def test_old_history_bounded_without_reordering(db):  # 25
    from backend.agent.graph import build_graph

    service = _service(db, max_history_messages=2)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    seen_contents = []

    class CapturingModel(DeterministicChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen_contents.append(
                [(type(m).__name__, m.content) for m in messages])
            return super()._generate(messages, stop, run_manager, **kwargs)

    capturing = CapturingModel()
    service._runtime._model = capturing
    service._runtime._graph = build_graph(capturing)
    for text in ("a", "b", "c"):
        await service.submit_turn(identity.agent_id, conversation.id, text)
    # Third turn sees only the two most recent stored rows, in order,
    # plus the current message: [user b, agent reply, user c].
    last = seen_contents[-1]
    assert [(kind, content) for kind, content in last] == [
        ("SystemMessage", service._runtime.config.system_instruction),
        ("HumanMessage", "b"),
        ("AIMessage", "test reply"),
        ("HumanMessage", "c"),
    ]
    full = await service.list_history(identity.agent_id, conversation.id)
    assert len(full) == 6  # database keeps everything


@pytest.mark.asyncio
async def test_model_failure_leaves_no_partial_pair(db):  # 26
    service = _service(db)
    service._runtime._model.fail_with = RuntimeError("provider down")
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    with pytest.raises(ModelInvocationError):
        await service.submit_turn(identity.agent_id, conversation.id, "hi")
    history = await service.list_history(identity.agent_id, conversation.id)
    assert history == []


@pytest.mark.asyncio
async def test_database_failure_rolls_back_both_messages(db):  # 27
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    calls = {"n": 0}
    original = ConversationMessageRepository.create

    async def flaky_create(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            from backend.persistence.errors import PersistenceError

            raise PersistenceError("simulated write failure")
        return await original(self, *args, **kwargs)

    ConversationMessageRepository.create = flaky_create
    try:
        with pytest.raises(PersistenceError):
            await service.submit_turn(identity.agent_id, conversation.id, "hi")
    finally:
        ConversationMessageRepository.create = original
    history = await service.list_history(identity.agent_id, conversation.id)
    assert history == []


@pytest.mark.asyncio
async def test_equal_timestamp_ordering_deterministic(db):  # 28
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    await service.submit_turn(identity.agent_id, conversation.id, "x")
    first = await service.list_history(identity.agent_id, conversation.id)
    second = await service.list_history(identity.agent_id, conversation.id)
    assert [row.id for row in first] == [row.id for row in second]
    assert [row.id for row in first] == sorted(row.id for row in first)


@pytest.mark.asyncio
async def test_concurrent_same_conversation_turns_serialized(db):  # 29
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    turns = await asyncio.gather(*[
        service.submit_turn(identity.agent_id, conversation.id, f"q{i}")
        for i in range(3)
    ])
    assert len(turns) == 3
    history = await service.list_history(identity.agent_id, conversation.id)
    assert len(history) == 6
    assert sorted(row.content for row in history if row.role == "user") == [
        "q0", "q1", "q2"]


@pytest.mark.asyncio
async def test_different_conversations_independent(db):  # 30
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    first = await service.create_conversation(identity.agent_id)
    second = await service.create_conversation(identity.agent_id)
    await service.submit_turn(identity.agent_id, first.id, "for-first")
    history = await service.list_history(identity.agent_id, second.id)
    assert history == []
    history = await service.list_history(identity.agent_id, first.id)
    assert [row.content for row in history] == ["for-first", "test reply"]


@pytest.mark.asyncio
async def test_unicode_emoji_round_trip(db):  # 31 (service path)
    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = await _owned_agent(session)
    conversation = await service.create_conversation(identity.agent_id)
    text = "caf\u00e9 \U0001f600 \u4e2d\u6587"
    turn = await service.submit_turn(identity.agent_id, conversation.id, text)
    assert turn.response.text == "test reply"
    history = await service.list_history(identity.agent_id, conversation.id)
    assert history[0].content == text
