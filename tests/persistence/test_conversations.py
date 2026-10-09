"""Conversation repository tests (real PostgreSQL)."""

import asyncio
import base64
import os
import pathlib
import types

import pytest
from alembic import command
from alembic.config import Config
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
    ConversationRepository,
)

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


async def _agent(session):
    identity = create_identity()
    raw = public_key_to_bytes(identity.public_key)
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    await AgentRepository(session).create(identity.agent_id, encoded)
    return identity.agent_id


@pytest.mark.asyncio
async def test_create_conversation(db):  # 34
    async with session_scope(db.sessions) as session:
        agent_id = await _agent(session)
        conversation = await ConversationRepository(session).create(
            agent_id, title="Planning"
        )
        assert conversation.id is not None
        assert conversation.title == "Planning"


@pytest.mark.asyncio
async def test_add_message(db):  # 35
    async with session_scope(db.sessions) as session:
        agent_id = await _agent(session)
        conversation = await ConversationRepository(session).create(agent_id)
        message = await ConversationMessageRepository(session).create(
            conversation.id, "user", "Hello there",
            meta={"source": "ui"},
        )
        assert message.id is not None
        assert message.role == "user"
        assert message.meta == {"source": "ui"}


@pytest.mark.asyncio
async def test_list_messages(db):  # 36
    async with session_scope(db.sessions) as session:
        agent_id = await _agent(session)
        conversation = await ConversationRepository(session).create(agent_id)
        messages = ConversationMessageRepository(session)
        await messages.create(conversation.id, "user", "first")
        await messages.create(conversation.id, "agent", "second")
        await messages.create(conversation.id, "user", "third")
        listed = await messages.list_by_conversation(conversation.id)
        assert [row.content for row in listed] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_remote_request_correlation_preserved(db):  # 37
    async with session_scope(db.sessions) as session:
        agent_id = await _agent(session)
        conversation = await ConversationRepository(session).create(agent_id)
        await ConversationMessageRepository(session).create(
            conversation.id, "agent", "Answer from peer",
            request_id="req_abc123",
        )
    async with db.sessions() as session:
        listed = await ConversationMessageRepository(session).list_by_conversation(
            conversation.id
        )
        assert [row.request_id for row in listed] == ["req_abc123"]


@pytest.mark.asyncio
async def test_invalid_role_rejected(db):
    async with session_scope(db.sessions) as session:
        agent_id = await _agent(session)
        conversation = await ConversationRepository(session).create(agent_id)
        with pytest.raises(PersistenceError):
            await ConversationMessageRepository(session).create(
                conversation.id, "gremlin", "hi"
            )


@pytest.mark.asyncio
async def test_conversation_title_update(db):
    async with session_scope(db.sessions) as session:
        agent_id = await _agent(session)
        conversation = await ConversationRepository(session).create(
            agent_id, title="Old"
        )
        updated = await ConversationRepository(session).update(
            conversation.id, "New"
        )
        assert updated is not None and updated.title == "New"
