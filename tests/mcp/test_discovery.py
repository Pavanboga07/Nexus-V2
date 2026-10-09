"""Discovery tests: real adapter behavior, schemas, failure modes."""

import pytest

from backend.mcp.config import MCPManagerConfig
from backend.mcp.errors import MCPConfigurationError, MCPDiscoveryError
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

    text = FastMCP("text")

    @text.tool
    def text_upper(value: str) -> str:
        """Uppercase text."""
        return value.upper()

    return math, text


def _config(servers, allowed):
    return MCPManagerConfig.from_dict({
        "mcpServers": {name: {"command": "python"} for name in servers},
        "allowedTools": allowed,
    })


@pytest.mark.asyncio
async def test_configured_server_exposes_tool():  # 11
    math, _ = _servers()
    manager = MCPManager(_config(["math"], ["math_add"]),
                         prebuilt_targets={"math": math})
    await manager.open()
    try:
        assert [tool.name for tool in manager.allowed_tools()] == ["math_add"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_actual_tool_schema_preserved():  # 12
    math, _ = _servers()
    manager = MCPManager(_config(["math"], ["math_add", "math_multiply"]),
                         prebuilt_targets={"math": math})
    await manager.open()
    try:
        by_name = {tool.name: tool for tool in manager.allowed_tools()}
        assert by_name["math_add"].args == {
            "a": {"type": "integer"}, "b": {"type": "integer"}}
        assert "Multiply" in (by_name["math_multiply"].description or "")
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_non_allowlisted_tool_unavailable():  # 14
    math, _ = _servers()
    manager = MCPManager(_config(["math"], ["math_add"]),
                         prebuilt_targets={"math": math})
    await manager.open()
    try:
        assert [tool.name for tool in manager.allowed_tools()] == ["math_add"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_empty_allowlist_exposes_zero_tools():  # 15
    math, _ = _servers()
    manager = MCPManager(_config(["math"], []),
                         prebuilt_targets={"math": math})
    await manager.open()
    try:
        assert manager.allowed_tools() == []
        assert manager.inventory() == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_unknown_allowlist_entry_fails_clearly():  # 16
    math, _ = _servers()
    manager = MCPManager(_config(["math"], ["nope_missing"]),
                         prebuilt_targets={"math": math})
    with pytest.raises(MCPConfigurationError, match="nope_missing"):
        await manager.open()


@pytest.mark.asyncio
async def test_duplicate_tool_names_unambiguous():  # 17
    math, text = _servers()

    async def _namespaced():
        from fastmcp import Client as FastMCPClient
        from fastmcp.client.group import ClientGroup
        from langchain.mcp import MCPAdapter

        group = ClientGroup(
            {"math": FastMCPClient(math), "text": FastMCPClient(text)})
        adapter = MCPAdapter(group)
        async with adapter:
            return [tool.name for tool in await adapter.list_tools()]

    names = await _namespaced()
    # Group keys prefix mechanically (even already-prefixed names).
    assert sorted(names) == [
        "math_math_add", "math_math_multiply", "text_text_upper"]

    # A same-name collision inside one flat list is rejected, not merged.
    manager = MCPManager(_config(["math"], ["math_add"]),
                         prebuilt_targets={"math": math})

    class _Dup:
        name = "math_add"

    manager._open = True
    manager._discovered = []
    with pytest.raises(MCPConfigurationError, match="duplicate"):
        manager._apply_allowlist([_Dup(), _Dup()])


@pytest.mark.asyncio
async def test_malformed_discovery_response_fails_safely():  # 19
    math, _ = _servers()
    manager = MCPManager(_config(["math"], ["math_add"]),
                         prebuilt_targets={"math": math})
    await manager.open()
    try:
        manager._open = True
        with pytest.raises(MCPDiscoveryError):
            manager._apply_allowlist([object()])
    finally:
        await manager.close()
