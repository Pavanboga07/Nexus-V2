"""Executor with MCP-provisioned tools: real manager, graph, execution.

Lives under tests/mcp (runs after the gateway isolation tests) and
imports FastMCP only inside test bodies, so the ``mcp`` package never
enters ``sys.modules`` before the gateway suite runs.
"""

import asyncio
import os
import pathlib
import types

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool

from backend.agent.config import AgentRuntimeConfig
from backend.agent.runtime import AgentRuntime
from backend.execution import RemoteExecutor
from backend.mcp.config import MCPManagerConfig
from backend.mcp.manager import MCPManager
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
)
from tests.execution.conftest import (
    Keys,
    ScriptedModel,
    seed_approved,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_mcp_exec",
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


@pytest.mark.asyncio
async def test_mcp_provisioned_tool_executes_in_remote_execution(db):
    from fastmcp import FastMCP

    keys = Keys()
    _, recipient, request_id = await seed_approved(
        db, keys, query="add twenty and twenty-two")
    calls = []
    server = FastMCP("calc")

    @server.tool
    def calc_add(a: int, b: int) -> int:
        """Add two numbers."""
        calls.append((a, b))
        return a + b

    config = MCPManagerConfig.from_dict(
        {"mcpServers": {"calc": {"command": "python"}},
         "allowedTools": ["calc_add"]})
    manager = MCPManager(config, prebuilt_targets={"calc": server})
    tools = await manager.open()
    try:
        assert [tool.name for tool in tools] == ["calc_add"]
        model = ScriptedModel()
        model.steps = [
            AIMessage(content="", tool_calls=[
                {"name": "calc_add", "args": {"a": 20, "b": 22},
                 "id": "call_1", "type": "tool_call"}]),
            AIMessage(content="forty-two"),
        ]
        runtime = AgentRuntime(
            AgentRuntimeConfig(), model=model, tools=tools)
        executor = RemoteExecutor(
            db.sessions, recipient.agent_id, keys, runtime)
        outcome = await executor.execute(request_id)
        assert outcome.status == "COMPLETED"
        assert outcome.executed is True
        assert calls == [(20, 22)]
        assert outcome.response_message_id is not None
    finally:
        await manager.close()
