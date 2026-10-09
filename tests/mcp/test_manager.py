"""Manager lifecycle tests: open/close/discovery plumbing (real adapter)."""

import asyncio

import pytest

from backend.mcp.config import MCPManagerConfig
from backend.mcp.errors import (
    MCPConfigurationError,
    MCPConnectionError,
    MCPDiscoveryError,
)
from backend.mcp.manager import MCPManager


def _math_server():
    from fastmcp import FastMCP

    server = FastMCP("math")

    @server.tool
    def math_add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    return server


def _config(**over):
    document = {"mcpServers": {"math": {"command": "python"}},
                "allowedTools": ["math_add"]}
    document.update(over)
    return MCPManagerConfig.from_dict(document)


@pytest.mark.asyncio
async def test_open_discovers_and_close_releases():
    manager = MCPManager(
        _config(), prebuilt_targets={"math": _math_server()})
    assert not manager.is_open
    tools = await manager.open()
    assert manager.is_open
    assert [tool.name for tool in tools] == ["math_add"]
    await manager.close()
    assert not manager.is_open
    await manager.close()  # idempotent


@pytest.mark.asyncio
async def test_open_is_idempotent():
    manager = MCPManager(
        _config(), prebuilt_targets={"math": _math_server()})
    first = await manager.open()
    second = await manager.open()
    assert [tool.name for tool in first] == [tool.name for tool in second]
    await manager.close()


@pytest.mark.asyncio
async def test_operations_require_open_manager():
    manager = MCPManager(
        _config(), prebuilt_targets={"math": _math_server()})
    with pytest.raises(MCPConnectionError):
        await manager.discover()
    with pytest.raises(MCPConnectionError):
        manager.allowed_tools()


@pytest.mark.asyncio
async def test_unknown_prebuilt_target_rejected():
    config = MCPManagerConfig.from_dict({"mcpServers": {}})
    with pytest.raises(MCPConfigurationError, match="prebuilt"):
        MCPManager(config, prebuilt_targets={"ghost": object()})


@pytest.mark.asyncio
async def test_connection_failure_reported():  # 18
    config = MCPManagerConfig.from_dict({
        "mcpServers": {"dead": {"command": "definitely-not-a-binary-xyz"}},
        "allowedTools": [],
    })
    manager = MCPManager(config)
    with pytest.raises((MCPConnectionError, MCPDiscoveryError)):
        await manager.open()
    await manager.close()


@pytest.mark.asyncio
async def test_inventory_has_no_secrets():
    manager = MCPManager(
        _config(), prebuilt_targets={"math": _math_server()})
    await manager.open()
    try:
        inventory = manager.inventory()
        assert inventory[0]["name"] == "math_add"
        assert inventory[0]["server"] == "math"
        assert "a" in inventory[0]["args"]
        assert set(inventory[0]) == {"name", "description", "server", "args"}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_concurrent_open_enters_single_adapter_set():
    """Concurrent open() calls share one adapter set (no orphans)."""
    from unittest import mock

    import langchain.mcp

    manager = MCPManager(
        _config(), prebuilt_targets={"math": _math_server()})
    real_adapter = langchain.mcp.MCPAdapter
    constructed = 0

    class CountingAdapter(real_adapter):
        def __init__(self, *args, **kwargs):
            nonlocal constructed
            constructed += 1
            super().__init__(*args, **kwargs)

    with mock.patch.object(
        langchain.mcp, "MCPAdapter", CountingAdapter
    ):
        results = await asyncio.gather(
            *[manager.open() for _ in range(8)]
        )
    try:
        names = [[tool.name for tool in tools] for tools in results]
        assert names == [["math_add"]] * 8
        assert constructed == 1
        assert manager.is_open
        assert len(manager._adapters) == 1
    finally:
        await manager.close()
    assert not manager.is_open


@pytest.mark.asyncio
async def test_failed_open_resets_state_and_allows_retry():
    """A failed open() leaves closed state; the same manager can retry."""
    from langchain.mcp import MCPAdapter

    manager = MCPManager(
        _config(), prebuilt_targets={"math": _math_server()})
    real_list_tools = MCPAdapter.list_tools
    calls = 0

    async def flaky_list_tools(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient discovery boom")
        return await real_list_tools(self)

    MCPAdapter.list_tools = flaky_list_tools
    try:
        with pytest.raises(MCPDiscoveryError) as excinfo:
            await manager.open()
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert not manager.is_open
        assert manager._adapters == []
        assert manager._discovered == []
        assert manager._servers_by_tool == {}
        tools = await manager.open()  # same manager, no reconstruction
        assert [tool.name for tool in tools] == ["math_add"]
        assert manager.is_open
    finally:
        MCPAdapter.list_tools = real_list_tools
        await manager.close()
    assert not manager.is_open
