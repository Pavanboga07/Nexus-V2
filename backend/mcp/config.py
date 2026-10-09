"""MCP configuration: operator-owned servers plus an explicit allowlist.

Configuration comes from local files or the ``NEXUS_MCP_CONFIG_PATH``
environment variable — never from user messages, model output, or
remote requests. Only two transports are supported: STDIO subprocesses
(commands from trusted local configuration only) and Streamable HTTP
URLs (HTTPS, except loopback development endpoints which may use HTTP).

Secrets are never stored here: server ``env`` entries take either
literal local values or ``env:VAR_NAME`` references resolved from the
process environment at connect time. A missing variable fails fast by
name; values are never logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .errors import MCPConfigurationError

ENV_REFERENCE_PREFIX = "env:"
CONFIG_ENV_VAR = "NEXUS_MCP_CONFIG_PATH"

_STDIO_KEYS = frozenset({"command", "args", "env"})
_HTTP_KEYS = frozenset({"url", "headers", "env"})
_TRANSPORTS = ("stdio", "streamable-http")


def _is_loopback(host: str) -> bool:
    """True for localhost names and loopback IP literals."""
    candidate = (host or "").lower().rstrip(".")
    if candidate in ("localhost",):
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ServerSpec:
    """One validated operator-configured MCP server."""

    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None

    def resolved_env(self, environ: Any = None) -> dict[str, str] | None:
        """Resolve ``env:`` references (missing names fail by name)."""
        if self.env is None:
            return None
        source = environ if environ is not None else os.environ
        resolved: dict[str, str] = {}
        for key, value in self.env.items():
            if isinstance(value, str) and value.startswith(ENV_REFERENCE_PREFIX):
                name = value[len(ENV_REFERENCE_PREFIX):]
                if not name or name not in source:
                    raise MCPConfigurationError(
                        f"server {self.name!r} needs environment "
                        f"variable {name!r}"
                    )
                resolved[key] = source[name]
            else:
                resolved[key] = value
        return resolved


def _validate_stdio(name: str, entry: dict[str, Any]) -> ServerSpec:
    unknown = set(entry) - _STDIO_KEYS - {"transport"}
    if unknown:
        raise MCPConfigurationError(
            f"server {name!r} has unknown fields: {sorted(unknown)}"
        )
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        raise MCPConfigurationError(
            f"server {name!r} needs a non-empty 'command' string"
        )
    args = entry.get("args", [])
    if not isinstance(args, list) or any(
        not isinstance(item, str) for item in args
    ):
        raise MCPConfigurationError(
            f"server {name!r} 'args' must be a list of strings"
        )
    env = entry.get("env")
    if env is not None and (
        not isinstance(env, dict)
        or any(not isinstance(k, str) or not isinstance(v, str)
               for k, v in env.items())
    ):
        raise MCPConfigurationError(
            f"server {name!r} 'env' must map strings to strings"
        )
    return ServerSpec(
        name=name, transport="stdio", command=command.strip(),
        args=tuple(args), env=dict(env) if env is not None else None,
    )


def _validate_http(name: str, entry: dict[str, Any]) -> ServerSpec:
    unknown = set(entry) - _HTTP_KEYS - {"transport"}
    if unknown:
        raise MCPConfigurationError(
            f"server {name!r} has unknown fields: {sorted(unknown)}"
        )
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip():
        raise MCPConfigurationError(
            f"server {name!r} needs a non-empty 'url' string"
        )
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise MCPConfigurationError(
            f"server {name!r} has an unsupported URL (use http(s))"
        )
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise MCPConfigurationError(
            f"server {name!r} must use HTTPS for non-loopback hosts"
        )
    headers = entry.get("headers")
    if headers is not None and (
        not isinstance(headers, dict)
        or any(not isinstance(k, str) or not isinstance(v, str)
               for k, v in headers.items())
    ):
        raise MCPConfigurationError(
            f"server {name!r} 'headers' must map strings to strings"
        )
    env = entry.get("env")
    if env is not None and (
        not isinstance(env, dict)
        or any(not isinstance(k, str) or not isinstance(v, str)
               for k, v in env.items())
    ):
        raise MCPConfigurationError(
            f"server {name!r} 'env' must map strings to strings"
        )
    return ServerSpec(
        name=name, transport="streamable-http", url=url.strip(),
        headers=dict(headers) if headers is not None else None,
        env=dict(env) if env is not None else None,
    )


@dataclass(frozen=True)
class MCPManagerConfig:
    """Validated local MCP configuration plus the tool allowlist."""

    servers: tuple[ServerSpec, ...] = ()
    allowed_tools: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> MCPManagerConfig:
        """Parse and strictly validate a ``mcpServers`` document."""
        if not isinstance(data, dict):
            raise MCPConfigurationError(
                "MCP configuration must be a JSON object"
            )
        unknown_top = set(data) - {"mcpServers", "allowedTools"}
        if unknown_top:
            raise MCPConfigurationError(
                f"unknown configuration sections: {sorted(unknown_top)}"
            )
        raw_servers = data.get("mcpServers", {})
        if not isinstance(raw_servers, dict):
            raise MCPConfigurationError("'mcpServers' must be an object")
        servers: list[ServerSpec] = []
        for name, entry in raw_servers.items():
            if not isinstance(name, str) or not name.strip():
                raise MCPConfigurationError(
                    "server names must be non-empty strings"
                )
            if not isinstance(entry, dict):
                raise MCPConfigurationError(
                    f"server {name!r} must be an object"
                )
            transport = entry.get("transport", "stdio")
            if transport == "stdio":
                servers.append(_validate_stdio(name, entry))
            elif transport == "streamable-http":
                servers.append(_validate_http(name, entry))
            else:
                raise MCPConfigurationError(
                    f"server {name!r} has unsupported transport "
                    f"{transport!r} (expected 'stdio' or 'streamable-http')"
                )
        raw_allowed = data.get("allowedTools", [])
        if not isinstance(raw_allowed, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in raw_allowed
        ):
            raise MCPConfigurationError(
                "'allowedTools' must be a list of non-empty strings"
            )
        if any("*" in item for item in raw_allowed):
            raise MCPConfigurationError(
                "wildcard allowlisting is not supported in V1"
            )
        if len(set(raw_allowed)) != len(raw_allowed):
            raise MCPConfigurationError(
                "'allowedTools' must not contain duplicates"
            )
        return cls(
            servers=tuple(servers), allowed_tools=tuple(raw_allowed)
        )

    @classmethod
    def from_file(cls, path: str) -> MCPManagerConfig:
        """Load configuration from a local operator-owned file."""
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError as exc:
            raise MCPConfigurationError(
                f"MCP configuration file not found: {path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise MCPConfigurationError(
                f"MCP configuration file is not valid JSON: {path}"
            ) from exc
        except OSError as exc:
            raise MCPConfigurationError(
                f"MCP configuration file unreadable: {path}"
            ) from exc
        return cls.from_dict(data)

    @classmethod
    def from_env(
        cls, variable: str = CONFIG_ENV_VAR, environ: Any = None
    ) -> MCPManagerConfig | None:
        """Load from ``NEXUS_MCP_CONFIG_PATH``; None when unset."""
        source = environ if environ is not None else os.environ
        path = source.get(variable)
        if not path:
            return None
        return cls.from_file(path)
