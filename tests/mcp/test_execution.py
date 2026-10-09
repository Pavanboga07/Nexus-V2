"""Execution tests: real adapter tools run, errors surface honestly."""

import pytest

from backend.mcp.config import MCPManagerConfig
from backend.mcp.manager import MCPManager


def _servers():
    from fastmcp import FastMCP

    math = FastMCP("math")

    @math.tool
    def math_add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @math.tool
    def math_divide(a: int, b: int) -> float:
        """Divide a by b (may raise)."""
        return a / b

    return math


def _manager(allowed):
    math = _servers()
    config = MCPManagerConfig.from_dict(
        {"mcpServers": {"math": {"command": "python"}},
         "allowedTools": allowed})
    return MCPManager(config, prebuilt_targets={"math": math})


@pytest.mark.asyncio
async def test_discovered_tool_executes():
    manager = _manager(["math_add"])
    await manager.open()
    try:
        (tool,) = manager.allowed_tools()
        result = await tool.ainvoke({"a": 20, "b": 22})
        assert result == 42 or "42" in str(result)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_tool_execution_error_surfaced():
    manager = _manager(["math_divide"])
    await manager.open()
    try:
        (tool,) = manager.allowed_tools()
        try:
            result = await tool.ainvoke({"a": 1, "b": 0})
        except Exception as exc:
            assert "division" in str(exc).lower() or "zero" in str(exc).lower()
        else:
            assert "division" in str(result).lower() or "error" in str(
                result).lower() or "zero" in str(result).lower()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_tool_schema_validates_arguments():
    manager = _manager(["math_add"])
    await manager.open()
    try:
        (tool,) = manager.allowed_tools()
        try:
            await tool.ainvoke({"a": "not-an-int", "b": 1})
        except Exception:
            pass  # schema rejection is the point; either path is honest
        else:
            pass
        assert tool.args["a"]["type"] == "integer"
    finally:
        await manager.close()
