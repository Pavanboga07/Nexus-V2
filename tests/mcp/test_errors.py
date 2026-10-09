"""MCP error taxonomy: distinct, chained, secret-free."""

import pytest

from backend.mcp.errors import (
    MCPConfigurationError,
    MCPConnectionError,
    MCPDiscoveryError,
    MCPError,
)


def test_hierarchy():
    for cls in (MCPConfigurationError, MCPConnectionError, MCPDiscoveryError):
        assert issubclass(cls, MCPError)
        assert issubclass(cls, Exception)


def test_chaining_and_safe_messages():
    cause = OSError("underlying transport blew up")
    error = MCPConnectionError("cannot connect MCP server (OSError)")
    error.__cause__ = cause
    assert error.__cause__ is cause
    for error in (
        MCPConfigurationError("allowlisted tools not discovered: ['x']"),
        MCPConnectionError("cannot connect MCP server (OSError)"),
        MCPDiscoveryError("tool discovery failed: ValueError"),
    ):
        text = str(error).lower()
        assert "sk-" not in text
        assert "bearer" not in text
        assert "api_key" not in text
