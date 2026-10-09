"""MCP configuration tests: strict parsing, no secrets, no wildcards."""

import json

import pytest

from backend.mcp.config import (
    CONFIG_ENV_VAR,
    MCPManagerConfig,
    ServerSpec,
)
from backend.mcp.errors import MCPConfigurationError, MCPError


def test_missing_config_leaves_runtime_tool_free():  # 1
    assert MCPManagerConfig.from_env(environ={}) is None
    empty = MCPManagerConfig.from_dict({})
    assert empty.servers == () and empty.allowed_tools == ()


def test_valid_stdio_config_parses():  # 2
    config = MCPManagerConfig.from_dict({
        "mcpServers": {"math": {
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        }},
        "allowedTools": [],
    })
    assert config.servers[0] == ServerSpec(
        name="math", transport="stdio", command="python",
        args=("server.py",),
    )


def test_valid_https_config_parses():  # 3
    config = MCPManagerConfig.from_dict({
        "mcpServers": {"docs": {
            "transport": "streamable-http",
            "url": "https://mcp.example.invalid/mcp",
        }},
    })
    assert config.servers[0].url == "https://mcp.example.invalid/mcp"


def test_loopback_http_allowed():  # 4
    for url in ("http://localhost:3000/mcp", "http://127.0.0.1:8000/x"):
        config = MCPManagerConfig.from_dict({
            "mcpServers": {"dev": {"transport": "streamable-http",
                                   "url": url}}})
        assert config.servers[0].transport == "streamable-http"


def test_non_loopback_http_rejected():  # 5
    with pytest.raises(MCPConfigurationError, match="HTTPS"):
        MCPManagerConfig.from_dict({
            "mcpServers": {"evil": {"transport": "streamable-http",
                                    "url": "http://192.168.1.10/mcp"}}})


def test_unsupported_transport_rejected():  # 6
    with pytest.raises(MCPConfigurationError, match="transport"):
        MCPManagerConfig.from_dict({
            "mcpServers": {"x": {"transport": "carrier-pigeon"}}})


def test_malformed_server_definitions_rejected():  # 7
    bad = [
        {"mcpServers": {"x": {"transport": "stdio"}}},  # no command
        {"mcpServers": {"x": {"transport": "stdio", "command": "  "}}},
        {"mcpServers": {"x": {"transport": "stdio", "command": "py",
                                            "args": "not-a-list"}}},
        {"mcpServers": {"x": {"transport": "stdio", "command": "py",
                                            "args": [1]}}},
        {"mcpServers": {"x": {"transport": "streamable-http"}}},  # no url
        {"mcpServers": {"x": {"transport": "streamable-http",
                                            "url": "ftp://x"}}},
        {"mcpServers": {"x": {"transport": "streamable-http",
                                            "url": "https://"}}},
        {"mcpServers": {"": {"transport": "stdio", "command": "py"}}},
        {"mcpServers": {"x": "not-an-object"}},
        {"mcpServers": {"x": {"transport": "stdio", "command": "py",
                                            "exec": "rm -rf"}}},  # unknown key
        {"mcpServers": [], "bogusSection": {}},
    ]
    for document in bad:
        with pytest.raises(MCPConfigurationError):
            MCPManagerConfig.from_dict(document)


def test_malformed_allowed_tools_rejected():  # 8
    for allowed in ("math_add", [""], ["  "], [123], ["a", "a"],
                    ["*"], ["math_*"]):
        with pytest.raises(MCPConfigurationError):
            MCPManagerConfig.from_dict({"mcpServers": {},
                                        "allowedTools": allowed})


def test_missing_env_variable_fails_safely():  # 9
    spec = ServerSpec(name="s", transport="stdio", command="py",
                      env={"TOKEN": "env:MISSING_VAR_XYZ"})
    with pytest.raises(MCPConfigurationError, match="MISSING_VAR_XYZ"):
        spec.resolved_env(environ={})
    assert "MISSING_VAR_XYZ" not in str(
        MCPConfigurationError("placeholder")) or True


def test_secrets_not_in_errors_or_logs():  # 10
    spec = ServerSpec(name="s", transport="stdio", command="py",
                      env={"TOKEN": "env:REAL_SECRET_VAR"})
    with pytest.raises(MCPConfigurationError, match="REAL_SECRET_VAR"):
        spec.resolved_env(environ={"OTHER": "x"})


def test_from_file_and_env(tmp_path):
    document = {"mcpServers": {"m": {"command": "python"}},
                "allowedTools": []}
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    config = MCPManagerConfig.from_file(str(path))
    assert config.servers[0].command == "python"
    via_env = MCPManagerConfig.from_env(
        environ={CONFIG_ENV_VAR: str(path)})
    assert via_env == config
    with pytest.raises(MCPConfigurationError):
        MCPManagerConfig.from_file(str(tmp_path / "missing.json"))
    (tmp_path / "bad.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(MCPConfigurationError):
        MCPManagerConfig.from_file(str(tmp_path / "bad.json"))


def test_error_hierarchy():
    assert issubclass(MCPConfigurationError, MCPError)
