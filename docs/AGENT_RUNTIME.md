# Nexus Local Agent Runtime (Component 7)

Dependencies: `langgraph==1.2.12`, `langchain==1.4.4`,
`langchain-core==1.6.9` (pinned in `pyproject.toml`). Provider
integrations (e.g. `langchain-openai`) stay optional and are installed
only for the configured provider. No new runtime dependencies.

## Agent Runtime responsibilities

`AgentRuntime` owns one compiled LangGraph workflow plus its chat
model: input validation, bounded history preparation, timed async
invocation, and validated structured results. It never touches the
network beyond the model call, never executes tools, never reads user
data stores, and never decides capability policy.

## LangGraph workflow

```text
START
→ generate_response
→ END
```

Without tools the graph above is the whole workflow. When the
runtime is constructed with an explicit tool set (see `docs/MCP.md`),
it becomes `START → generate_response → ToolNode → generate_response
→ END`: the model is bound once, unknown tool calls are rejected
before execution, results return as tool messages, and over-long
result text is cut with a marker. `AgentRuntime(config, model, tools)`
accepts any explicit subset, so future request-scoped runtimes reuse
this exact engine with narrower permission.

`AgentState` carries `system_instruction`, ordered `history`
(LangChain messages), `user_input`, and output `response_text`. The
`generate_response` node builds `[SystemMessage, *history,
HumanMessage]`, awaits the pre-built model exactly once, rejects
non-assistant/tool-call/empty output, and returns the text. The model
is bound with `functools.partial` at build time — state carries no
handles, nodes read no configuration. The graph compiles once per
`AgentRuntime` and is reused across turns. No checkpointer: each turn
is a single stateless invocation over application-persisted history.

## Model configuration

`AgentRuntimeConfig` (frozen dataclass, validated on construction):

- `model`: `provider:model-id` (e.g. `openai:gpt-4o`), required for
  real execution — no placeholder fallback, ever.
- `temperature`, `max_output_tokens`: forwarded only when set
  ("where supported" — a provider that rejects them fails fast at
  build time with `ModelConfigurationError`).
- `request_timeout_s` (default 60): enforced by the runtime around
  invocation, not by provider parameters.
- `max_input_chars` (default 65536): over-limit input is rejected,
  never silently truncated.
- `max_history_messages` (default 20): tail-kept in order.
- `system_instruction`: one fixed instruction, separate from content.

`AgentRuntimeConfig.from_env()` reads `NEXUS_LLM_MODEL`,
`NEXUS_LLM_TEMPERATURE`, `NEXUS_LLM_MAX_TOKENS`,
`NEXUS_LLM_TIMEOUT_S`, `NEXUS_LLM_MAX_INPUT_CHARS`,
`NEXUS_LLM_MAX_HISTORY`, `NEXUS_LLM_SYSTEM_INSTRUCTION`. Missing model
fails at build with an actionable error naming the variable.

## Supported provider setup

Any provider supported by `langchain.chat_models.init_chat_model`
works; install only its integration package
(`langchain-openai` for `openai:…`, `langchain-anthropic` for
`anthropic:…`, `langchain-google-genai` for `google_genai:…`, …).
Without it, construction raises `ModelConfigurationError` naming the
package. API keys live in the provider's own environment variables —
never in source, config files, logs, or errors.

## Public runtime interfaces

- `AgentRuntime(config, model=None)` — injected model or factory-built;
  `.run(user_input, history=None, *, system_instruction=None)` →
  `AssistantResponse(text, model_name)`; `.graph`, `.config` exposed.
- `ConversationService(sessions, runtime)` — `create_conversation`,
  `get_conversation`, `list_history`, `submit_turn` →
  `ConversationTurn(conversation_id, user_message_id,
  assistant_message_id, response, created_at)`.

## Local conversation service

Turns follow load(short txn) → validate → run (no open transaction)
→ persist user+assistant atomically → return. Generation failure
persists nothing, so retry is safe. Conversations belong to one
locally owned, active agent (`owner_user_id = NULL` means known
remote peer and is rejected, as are foreign-agent and missing
conversations). Concurrent turns on one conversation serialize on a
per-conversation lock (single-process; cross-process coordination is
explicitly out of scope).

## Conversation persistence behavior

PostgreSQL `conversations`/`conversation_messages` remain the source
of truth; the `id` column is the deterministic tiebreaker for equal
timestamps. The DB keeps full history while the model sees only the
bounded tail. Roles map strictly: `user`→`HumanMessage`,
`agent`→`AIMessage`; anything else is rejected, never converted.

## Context limits

`max_input_chars` (reject over), `max_history_messages` (tail-kept,
order-preserved), `request_timeout_s` (runtime-enforced),
`max_output_tokens` (provider-forwarded). No token-counting libraries.

## Error categories

`ModelConfigurationError` (missing/malformed model, missing
provider package), `InvalidConfigurationError` (bad limits),
`InvalidInputError` (input/roles), `ConversationNotFoundError`,
`ConversationMismatchError`, `AgentNotAvailableError` (inactive or
remote peer), `ModelInvocationError` (call failure/timeout, chained),
`InvalidModelOutputError` (empty/tool-call output), plus the reused
persistence errors. No retries, no fallback answers, no secret-bearing
messages; logs carry ids and categories only.

## How deterministic tests work

Every behavioral test injects a `DeterministicChatModel`
(`BaseChatModel` subclass with canned replies, call counting, and
failure modes) — no credentials, no network. The suite pins single
invocation, ordering, system separation, tool refusal, validation,
isolation, atomicity, limits, and transaction discipline.

## How to run an optional real-model smoke test

Set `NEXUS_LLM_MODEL=provider:model-id` plus the provider's API key
and `NEXUS_RUN_LLM_SMOKE=1`, then run
`tests/agent/test_integration.py::test_optional_real_model_smoke`.
It is skipped otherwise and never runs in the ordinary suite.

## Why checkpointers, long-term stores, MCP, and remote execution are deferred

One stateless invocation per turn over durable application history
needs no checkpointer or store; adding them now would duplicate the
source of truth. MCP/tools arrive in Component 8, inbound
remote-request execution in Component 10 — both will reuse this exact
runtime behind their own gates, never a second engine.

## What this component explicitly does not do

No protocol/cryptography, Gateway, request lifecycle, approvals, MCP,
tools, REST, WebSockets, frontend, auth, capability decisions, vector
storage, embeddings, semantic memory, checkpointers, or second
schemas/engines.
