"""Allowlist tests: exact-match filtering as the security boundary."""

import pytest

from backend.mcp.config import MCPManagerConfig
from backend.mcp.errors import MCPConfigurationError
from backend.mcp.manager import MCPManager


def _servers():
    from fastmcp import FastMCP

    math = FastMCP("math")

    @math.tool
    def math_add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @math.tool
    def math_multiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b

    @math.tool
    def math_secret(a: int) -> int:
        """A tool the operator never allowlists."""
        return a

    return math


def _manager(allowed):
    math = _servers()
    config = MCPManagerConfig.from_dict(
        {"mcpServers": {"math": {"command": "python"}},
         "allowedTools": allowed})
    return MCPManager(config, prebuilt_targets={"math": math})


@pytest.mark.asyncio
async def test_configured_allowlisted_tool_available():  # 13
    manager = _manager(["math_add", "math_multiply"])
    assert await manager.open() is not None
    try:
        assert sorted(tool.name for tool in manager.allowed_tools()) == [
            "math_add", "math_multiply"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sensitive_tool_stays_hidden():
    manager = _manager(["math_add"])
    await manager.open()
    try:
        assert "math_secret" not in [
            tool.name for tool in manager.allowed_tools()]
        full = await manager.discover()
        assert "math_secret" in [tool.name for tool in full]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_no_wildcards_or_prefix_matching():
    manager = _manager(["math_add"])
    await manager.open()
    try:
        names = [tool.name for tool in manager.allowed_tools()]
        assert "math_multiply" not in names
        assert "math_secret" not in names
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_allowlist_validated_against_discovery():
    manager = _manager(["math_add", "ghost_tool"])
    with pytest.raises(MCPConfigurationError, match="ghost_tool"):
        await manager.open()
    await manager.close()
