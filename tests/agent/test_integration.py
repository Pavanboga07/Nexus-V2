"""Integration: isolation, transaction discipline, optional live smoke."""

import asyncio
import ast
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

from backend.agent.config import AgentRuntimeConfig
from backend.agent.conversation_service import ConversationService
from backend.agent.runtime import AgentRuntime
from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.users import UserRepository

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENT_ROOT = ROOT / "backend" / "agent"
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

    @property
    def _llm_type(self) -> str:
        return "deterministic-test-model"

    def _generate(self, messages: list, stop: Any = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.canned))])


def _service(db, **over):
    config = AgentRuntimeConfig(**over)
    return ConversationService(
        db.sessions, AgentRuntime(config, model=DeterministicChatModel()))


def _agent_imports():
    modules = set()
    for path in AGENT_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
    return modules


def test_dependency_import_isolation_remains_intact():  # 36
    modules = _agent_imports()
    assert "langgraph" in modules  # genuinely used, not decorative
    assert "langchain_core" in modules
    for banned in ("gateway", "mcp", "frontend"):
        assert banned not in modules, banned

    # NOTE: no sys.modules assertion here by design. Pytest imports
    # every test module at collection time, so gateway (imported by
    # gateway tests) is legitimately present in a full-suite process;
    # the AST scans are the meaningful isolation proofs.
    roots = set()
    for path in AGENT_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.startswith("backend.")):
                roots.add(node.module.split(".")[1])
    assert roots <= {"persistence"}, roots


@pytest.mark.asyncio
async def test_no_transaction_open_during_model_invocation(db):  # 33
    import backend.agent.conversation_service as service_module

    service = _service(db)
    async with session_scope(db.sessions) as session:
        identity = create_identity()
        owner = await UserRepository(session).create()
        await AgentRepository(session).create(
            identity.agent_id,
            base64.urlsafe_b64encode(
                public_key_to_bytes(identity.public_key)).rstrip(b"=").decode("ascii"),
            owner_user_id=owner.id,
        )
    conversation = await service.create_conversation(identity.agent_id)

    windows = []
    real_scope = service_module.session_scope

    from contextlib import asynccontextmanager
    import itertools
    import time

    order = itertools.count()

    @asynccontextmanager
    async def tracking_scope(factory):
        windows.append(("open", next(order)))
        try:
            async with real_scope(factory) as session:
                yield session
        finally:
            windows.append(("close", next(order)))

    model_windows = []

    class TimedModel(DeterministicChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            model_windows.append(("call", next(order)))
            return super()._generate(messages, stop, run_manager, **kwargs)

    timed = TimedModel()
    service._runtime._model = timed
    from backend.agent.graph import build_graph

    service._runtime._graph = build_graph(timed)
    service_module.session_scope = tracking_scope
    try:
        await service.submit_turn(identity.agent_id, conversation.id, "hi")
    finally:
        service_module.session_scope = real_scope
    calls = [moment for kind, moment in model_windows if kind == "call"]
    assert len(calls) == 1
    # Every model call must fall outside every open transaction window.
    spans = []
    pending = None
    for kind, moment in windows:
        if kind == "open":
            pending = moment
        else:
            spans.append((pending, moment))
            pending = None
    for call in calls:
        assert not any(start < call < end for start, end in spans), (
            "model invoked inside an open transaction")


@pytest.mark.asyncio
async def test_optional_real_model_smoke():
    """Live-provider smoke: only with NEXUS_LLM_MODEL + explicit opt-in."""
    if os.environ.get("NEXUS_RUN_LLM_SMOKE") != "1":
        pytest.skip("set NEXUS_RUN_LLM_SMOKE=1 with provider credentials")
    if not os.environ.get("NEXUS_LLM_MODEL"):
        pytest.skip("set NEXUS_LLM_MODEL=provider:model-id")
    from backend.agent.model_factory import build_chat_model

    config = AgentRuntimeConfig.from_env()
    model = build_chat_model(config)
    runtime = AgentRuntime(config, model=model)
    response = await runtime.run("Reply with exactly: smoke-ok")
    assert response.text.strip()
