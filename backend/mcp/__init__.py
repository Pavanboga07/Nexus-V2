"""Nexus MCP integration (Component 8): configured tools, explicit trust.

Servers are operator-configured; only allowlisted tools reach runtimes.
"""

from .config import (
    CONFIG_ENV_VAR,
    ENV_REFERENCE_PREFIX,
    MCPManagerConfig,
    ServerSpec,
)
from .errors import (
    MCPConfigurationError,
    MCPConnectionError,
    MCPDiscoveryError,
    MCPError,
)
from .manager import MCPManager

__all__ = [
    "CONFIG_ENV_VAR",
    "ENV_REFERENCE_PREFIX",
    "MCPManagerConfig",
    "ServerSpec",
    "MCPError",
    "MCPConfigurationError",
    "MCPConnectionError",
    "MCPDiscoveryError",
    "MCPManager",
]
