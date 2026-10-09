"""MCP manager: explicit lifecycle over pinned `langchain.mcp` primitives.

One manager owns its adapters for its whole lifetime: ``open()`` enters
each adapter context and discovers tools once; ``close()`` releases in
reverse. Tools are fetched while open and reused across turns — the
manager (and therefore its tools) must outlive every runtime using
them. Single-server setups adapt directly (natural tool names);
multi-server setups go through one ``ClientGroup`` (server-key
namespacing, e.g. ``math_add``). In every case only allowlisted names
survive, matched verbatim against adapter-returned names.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .config import MCPManagerConfig, ServerSpec
from .errors import (
    MCPConfigurationError,
    MCPConnectionError,
    MCPDiscoveryError,
    MCPError,
)

logger = logging.getLogger("nexus.mcp")


class MCPManager:
    """Owns MCP connections and the allowlisted tool set."""

    def __init__(
        self,
        config: MCPManagerConfig,
        *,
        prebuilt_targets: dict[str, Any] | None = None,
        environ: Any = None,
    ) -> None:
        """Create a closed manager.

        ``prebuilt_targets`` maps server names to ready FastMCP
        servers/clients. It is a test and embedding seam: production
        configuration uses transport definitions only.
        """
        self._config = config
        self._prebuilt = dict(prebuilt_targets) if prebuilt_targets else {}
        unknown_prebuilt = set(self._prebuilt) - {
            spec.name for spec in config.servers
        }
        if unknown_prebuilt:
            raise MCPConfigurationError(
                "prebuilt targets without configuration: "
                f"{sorted(unknown_prebuilt)}"
            )
        self._environ = environ
        self._adapters: list[Any] = []
        self._discovered: list[Any] = []
        self._servers_by_tool: dict[str, str] = {}
        self._open = False
        # Serializes initialization and shutdown so concurrent open()
        # calls yield exactly one tracked adapter set and close() can
        # never slip between another call's creation and registration.
        self._lifecycle_lock = asyncio.Lock()
        # Serializes initialization and shutdown so concurrent open()
        # calls yield exactly one tracked adapter set and close() can
        # never slip between another call's creation and registration.
        self._lifecycle_lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        """True between successful ``open()`` and ``close()``."""
        return self._open

    @property
    def config(self) -> MCPManagerConfig:
        """The validated operator configuration."""
        return self._config

    async def open(self) -> list[Any]:
        """Connect, discover, and validate; return the allowlisted tools.

        Idempotent while open: repeated calls return the cached set
        without new connections. Concurrent calls serialize on the
        lifecycle lock with a second open-check, so losing calls reuse
        the winner's adapters instead of entering untracked ones. The
        manager reports open only after validation succeeds; any
        failure (including cancellation) releases what this attempt
        created, resets tracking state, and re-raises the original
        exception.
        """
        if self._open:
            return self.allowed_tools()
        async with self._lifecycle_lock:
            if self._open:
                return self.allowed_tools()
            try:
                from langchain.mcp import MCPAdapter
            except ImportError as exc:
                raise MCPConnectionError(
                    "langchain MCP integration is not installed"
                ) from exc
            adapters: list[Any] = []
            try:
                if len(self._config.servers) == 1:
                    spec = self._config.servers[0]
                    adapters.append(
                        MCPAdapter(self._target_for(spec)))
                else:
                    from fastmcp.client.group import ClientGroup
                    from fastmcp import Client as FastMCPClient

                    members = {
                        spec.name: FastMCPClient(self._target_for(spec))
                        for spec in self._config.servers
                    }
                    adapters.append(MCPAdapter(ClientGroup(members)))
                for adapter in adapters:
                    try:
                        await adapter.__aenter__()
                    except Exception as exc:
                        # Connection phase only: type name, never the raw
                        # message (transports may echo commands, paths, URLs).
                        raise MCPConnectionError(
                            "cannot connect MCP server "
                            f"({type(exc).__name__})"
                        ) from exc
                discovered = await self._discover_from(adapters)
                tools = self._apply_allowlist_to(discovered)
                self._adapters = adapters
                self._discovered = discovered
                self._servers_by_tool = self._attribute_servers(discovered)
                self._open = True
                return tools
            except BaseException:
                await self._close_adapters(adapters)
                raise

    async def close(self) -> None:
        """Release adapters in reverse; safe to call repeatedly."""
        async with self._lifecycle_lock:
            adapters, self._adapters = self._adapters, []
            self._open = False
            self._discovered = []
            self._servers_by_tool = {}
        await self._close_adapters(adapters)

    @staticmethod
    async def _close_adapters(adapters: list[Any]) -> None:
        for adapter in reversed(adapters):
            try:
                await adapter.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - shutdown must not fail
                logger.warning("MCP adapter close failed: %s", type(exc).__name__)

    async def discover(self) -> list[Any]:
        """All discovered tools (requires an open manager)."""
        if not self._open:
            raise MCPConnectionError("MCP manager is not open")
        return await self._discover_locked()

    async def startup(self) -> list[Any]:
        """Convenience: :meth:`open` (idempotent wrapper)."""
        return await self.open()

    def allowed_tools(self) -> list[Any]:
        """Discovered tools filtered by the explicit allowlist."""
        return self._apply_allowlist(self._discovered)

    def inventory(self) -> list[dict[str, Any]]:
        """Safe diagnostics: names, servers, descriptions, schemas."""
        items = []
        for tool in self.allowed_tools():
            items.append({
                "name": tool.name,
                "description": tool.description or "",
                "server": self._servers_by_tool.get(tool.name, ""),
                "args": dict(tool.args or {}),
            })
        return items

    def _target_for(self, spec: ServerSpec) -> Any:
        """Transport target for one server (prebuilt seam or config)."""
        if spec.name in self._prebuilt:
            return self._prebuilt[spec.name]
        environ = self._environ if self._environ is not None else os.environ
        if spec.transport == "stdio":
            resolved = spec.resolved_env(environ)
            target: dict[str, Any] = {
                "command": spec.command,
                "args": list(spec.args),
            }
            if resolved is not None:
                target["env"] = resolved
            return {"mcpServers": {spec.name: target}}
        if spec.transport == "streamable-http":
            # Resolve env eagerly so missing variables fail fast by
            # name even though remote transports rarely need it.
            spec.resolved_env(environ)
            remote: dict[str, Any] = {"url": spec.url}
            if spec.headers:
                remote["headers"] = dict(spec.headers)
            return {"mcpServers": {spec.name: remote}}
        raise MCPConfigurationError(
            f"server {spec.name!r} has unsupported transport "
            f"{spec.transport!r}"
        )

    async def _discover_from(self, adapters: list[Any]) -> list[Any]:
        """Run discovery across the given adapters (no state touched)."""
        discovered: list[Any] = []
        for adapter in adapters:
            try:
                tools = await adapter.list_tools()
            except MCPError:
                raise
            except Exception as exc:
                raise MCPDiscoveryError(
                    f"tool discovery failed: {type(exc).__name__}"
                ) from exc
            if not isinstance(tools, list):
                raise MCPDiscoveryError(
                    "tool discovery returned an unusable result"
                )
            discovered.extend(tools)
        return discovered

    async def _discover_locked(self) -> list[Any]:
        """Run discovery across open adapters (caller holds lifecycle)."""
        if not self._adapters:
            raise MCPConnectionError("MCP manager is not open")
        discovered = await self._discover_from(self._adapters)
        self._discovered = discovered
        self._servers_by_tool = self._attribute_servers(discovered)
        return list(discovered)

    def _attribute_servers(self, tools: list[Any]) -> dict[str, str]:
        """Map each tool name to its server (longest-key match)."""
        keys = sorted(
            (spec.name for spec in self._config.servers), key=len, reverse=True
        )
        mapping: dict[str, str] = {}
        for tool in tools:
            name = getattr(tool, "name", "")
            mapping[name] = ""
            for key in keys:
                if name == key or name.startswith(key + "_"):
                    mapping[name] = key
                    break
            if len(keys) == 1 and not mapping[name]:
                mapping[name] = keys[0]
        return mapping

    def _apply_allowlist_to(self, discovered: list[Any]) -> list[Any]:
        """Filter to exactly the allowlisted names (no state touched)."""
        names = [
            getattr(tool, "name", None) for tool in discovered
        ]
        if any(not isinstance(name, str) or not name for name in names):
            raise MCPDiscoveryError(
                "discovered tool without a usable name"
            )
        seen: set[str] = set()
        duplicates: set[str] = set()
        for name in names:
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        if duplicates:
            raise MCPConfigurationError(
                f"duplicate discovered tool names: {sorted(duplicates)}; "
                "server namespacing is required"
            )
        by_name = dict(zip(names, discovered))
        unknown = [
            item for item in self._config.allowed_tools if item not in by_name
        ]
        if unknown:
            raise MCPConfigurationError(
                f"allowlisted tools not discovered: {unknown}"
            )
        return [by_name[item] for item in self._config.allowed_tools]

    def _apply_allowlist(self, discovered: list[Any]) -> list[Any]:
        """Filter to exactly the allowlisted names (strict, no wildcards)."""
        if not self._open:
            raise MCPConnectionError("MCP manager is not open")
        return self._apply_allowlist_to(discovered)
