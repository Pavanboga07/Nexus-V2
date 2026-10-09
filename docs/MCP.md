# Nexus MCP Integration (Component 8)

Dependencies: `langchain[mcp]==1.4.4` (beta `langchain.mcp.MCPAdapter`),
`fastmcp==4.1.0` (transports), resolved `mcp==2.3.0`. No standalone
adapter packages, no provider bundles, no new frameworks.

## MCP's purpose in Nexus

MCP is the only sanctioned path for external tools: LangGraph
orchestrates, the model decides *when* a tool helps, MCP servers
provide the capability. Nexus itself never implements Gmail, GitHub,
Calendar, browser, or any other service — operators configure servers,
and only explicitly allowlisted tools reach the model.

## The selected installed integration and version

`langchain.mcp.MCPAdapter` (beta) over FastMCP transports.
`MCPAdapter(target)` accepts a FastMCP server object, a single-server
transport mapping, a remote URL string (http/https only — anything
else is rejected rather than risk launching local execution), or a
`ClientGroup` for multi-server setups. It is an async context manager:
connections live inside `open()`/`close()`. `list_tools()` returns
async LangChain `StructuredTool` objects with real JSON schemas.

## Local STDIO server configuration

```json
{"mcpServers": {
  "math": {"transport": "stdio",
           "command": "python",
           "args": ["path/to/operator_owned_math_server.py"]}},
 "allowedTools": ["math_add"]}
```

`command` must be a non-empty string, `args` a list of strings; both
come only from trusted local configuration — never from chat text,
model output, or remote requests (no shell strings are ever built
from untrusted input). See `config/mcp_servers.example.json`
(placeholders only, nothing real, nothing secret).

## Remote Streamable HTTP configuration

```json
{"mcpServers": {
  "docs": {"transport": "streamable-http",
           "url": "https://mcp.example.invalid/mcp"}},
 "allowedTools": ["docs_search"]}
```

HTTPS is required except loopback development endpoints
(`localhost`, `127.0.0.0/8`, `::1`), which may use plain HTTP.
Other schemes, missing hosts, and malformed URLs are rejected.
Server URLs never come from model arguments or remote requests.

## Explicit tool allowlisting

Discovery is not permission: `allowedTools` names the exact tools one
runtime may use, matched verbatim against adapter-returned names.
Unknown entries fail loudly, empty/missing lists expose zero tools,
wildcards are rejected, duplicates across the final set are rejected.
The adapter receives only transport fields it understands —
application-only `allowedTools` never leaks into transport config.

## Tool discovery

`MCPManager.open()` connects, runs `list_tools()` once, and caches
the allowlisted set for the manager's lifetime (no per-turn
reconnection). The manager must outlive every runtime using its
tools; `close()` releases adapters in reverse and is idempotent.

## Tool naming and namespacing

Single-server setups keep natural names (`math_add`); multi-server
setups go through one `ClientGroup`, whose keys mechanically prefix
names (`math` + `add` → `math_add`, even when already prefixed).
Allowlist entries use exactly these names. Collisions in a flat
discovery result are a configuration error, not a silent merge.

## Credential handling

Server `env` entries take literal local values or `env:VAR_NAME`
references resolved from the process environment at connect time; a
missing variable fails fast naming only the variable. Values are
never logged (only counts/names of resolved entries appear nowhere —
not even names of secret variables are echoed beyond the missing-name
error, which names the *variable*, never its value). Remote `headers`
pass through opaquely for operator-owned tokens; they never appear in
errors, inventory, or logs. No OAuth flows or credential stores are
implemented — anything beyond client-supported bearer configuration
is reported as a limitation, not worked around.

## Security boundaries

- Operator configuration is the only trust root for servers and
  tools; server descriptions/annotations are untrusted data.
- The allowlist is enforced in code (router pre-check + ToolNode),
  never by prompt text; the LLM never decides its own scope.
- Tool results stay tool results: they cannot become system
  instructions, cannot edit the system message, cannot widen the
  tool set, and cannot trigger unrelated tools.
- Configuring a tool means the operator exposed that capability to
  the local runtime. Read-only-looking metadata proves nothing about
  safety. Future remote invocation needs its own request-scoped
  allowlist (see below), never this full set.
- Tools needing mid-call human input (elicitation) fail explicitly
  in plain graphs instead of hanging; interactive resume is deferred.

## LangGraph tool execution

```
START
→ generate_response
→ ToolNode when needed
→ generate_response
→ END
```

The model is bound once with the runtime's tools; unknown tool calls
are rejected in the router before execution; `BoundedToolNode`
executes through stock ToolNode semantics with over-long result text
cut (marked) to protect context. Tool errors arrive as tool-result
messages for the model to handle, never as fabricated answers.

## Error handling

`MCPConfigurationError` (bad config, unknown allowlist entries,
missing env vars), `MCPConnectionError` (unreachable servers),
`MCPDiscoveryError` (bad discovery results) — all chained, all
secret-free. Tool-execution failures surface as tool results;
model-after-tool failures stay `ModelInvocationError`. No retries,
no silent fallbacks.

## How to add another MCP server

1. Add an entry under `mcpServers` in the operator config file
   (STDIO command or HTTPS URL).
2. Add exactly the tool names `list_tools()` reports to
   `allowedTools`.
3. Point `NEXUS_MCP_CONFIG_PATH` at the file; construct
   `MCPManager` → `open()` → pass `manager.allowed_tools()` into
   `AgentRuntime(..., tools=...)`.

## How to run the deterministic tests

`python -m pytest tests/mcp/ tests/agent/` — in-process FastMCP
servers plus scripted models; no network, no credentials. Real STDIO
subprocess servers are also exercised from temp scripts (never
committed).

## Which tool actions are exposed to local chat

Exactly the operator's `allowedTools` for that runtime instance —
nothing discovered implicitly, nothing inherited globally.

## Why remote execution must later use a request-specific permitted tool set

A future service will construct runtimes with a narrower tool list
per approved request (e.g. only `calendar_availability`, never
`email_send`). The constructor already accepts any explicit subset,
so no redesign is needed — only the authorization that chooses it.

## What Component 8 does NOT implement

No REST, frontend, inbound remote-query handling, approvals, Gateway
dispatch, capability authorization, user auth, discovery, vector
stores, embeddings, OAuth flows, or remote execution. No MCP server
implementations of its own.
