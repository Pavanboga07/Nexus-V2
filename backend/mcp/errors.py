"""MCP error model (safe messages, chained causes, no secrets)."""

from __future__ import annotations


class MCPError(Exception):
    """Base class for MCP integration failures."""


class MCPConfigurationError(MCPError):
    """Local MCP configuration is missing, malformed, or unsafe."""


class MCPConnectionError(MCPError):
    """An MCP server could not be reached or handshake failed."""


class MCPDiscoveryError(MCPError):
    """Tool discovery failed or returned unusable results."""
