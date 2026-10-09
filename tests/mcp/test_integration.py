"""Manager→runtime integration: real MCP tools through the real graph."""

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from backend.agent.config import AgentRuntimeConfig
from backend.agent.errors import InvalidModelOutputError, ModelInvocationError
from backend.agent.runtime import AgentRuntime
from backend.mcp.config import MCPManagerConfig
from backend.mcp.manager import MCPManager


class ScriptedModel(BaseChatModel):
    steps: Any = None
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-model"

    def bind_tools(self, tools, **kwargs):
        self._bound = sorted(tool.name for tool in tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])


def _math_server():
    from fastmcp import FastMCP

    server = FastMCP("calc")

    @server.tool
    def calc_add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    return server


async def _runtime_with_tools(steps):
    config = MCPManagerConfig.from_dict(
        {"mcpServers": {"calc": {"command": "python"}},
         "allowedTools": ["calc_add"]})
    manager = MCPManager(config, prebuilt_targets={"calc": _math_server()})
    tools = await manager.open()
    model = ScriptedModel()
    model.steps = list(steps)
    runtime = AgentRuntime(AgentRuntimeConfig(), model=model, tools=tools)
    return manager, runtime, model


@pytest.mark.asyncio
async def test_tool_round_trip_through_graph():
    manager, runtime, model = await _runtime_with_tools([
        AIMessage(content="", tool_calls=[
            {"name": "calc_add", "args": {"a": 20, "b": 22},
             "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="forty-two"),
    ])
    try:
        assert [tool.name for tool in runtime.tools] == ["calc_add"]
        response = await runtime.run("add twenty and twenty-two")
        assert response.text == "forty-two"
        assert model.calls == 2
        assert model._bound == ["calc_add"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_multi_step_tool_loop():
    manager, runtime, model = await _runtime_with_tools([
        AIMessage(content="", tool_calls=[
            {"name": "calc_add", "args": {"a": 1, "b": 2},
             "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[
            {"name": "calc_add", "args": {"a": 3, "b": 4},
             "id": "call_2", "type": "tool_call"}]),
        AIMessage(content="three and seven"),
    ])
    try:
        response = await runtime.run("add pairs")
        assert response.text == "three and seven"
        assert model.calls == 3
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_non_allowlisted_call_rejected_before_execution():
    manager, runtime, model = await _runtime_with_tools([
        AIMessage(content="", tool_calls=[
            {"name": "calc_evil", "args": {},
             "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="unreachable"),
    ])
    try:
        with pytest.raises(InvalidModelOutputError, match="calc_evil"):
            await runtime.run("do evil")
        assert model.calls == 1  # routed away before any ToolNode run
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_tool_error_reaches_model_as_result():
    manager, runtime, model = await _runtime_with_tools([
        AIMessage(content="", tool_calls=[
            {"name": "calc_add", "args": {"a": "x", "b": 1},
             "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="that input was invalid"),
    ])
    try:
        response = await runtime.run("add")
        assert response.text == "that input was invalid"
        assert model.calls == 2
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_model_failure_after_tool_not_converted():
    manager, runtime, model = await _runtime_with_tools([
        AIMessage(content="", tool_calls=[
            {"name": "calc_add", "args": {"a": 1, "b": 1},
             "id": "call_1", "type": "tool_call"}]),
        ConnectionError("provider vanished mid-loop"),
    ])
    try:
        with pytest.raises(ModelInvocationError):
            await runtime.run("add")
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_runtime_without_tools_unchanged():
    from tests.agent.test_runtime import DeterministicChatModel

    runtime = AgentRuntime(AgentRuntimeConfig(),
                           model=DeterministicChatModel())
    assert runtime.tools == ()
    response = await runtime.run("plain hello")
    assert response.text == "test reply"
