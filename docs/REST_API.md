# Nexus REST API (Component 12)

Versioned HTTP access (`/api/v1`) over the existing Nexus services for
the upcoming frontend (Component 14) and application events
(Component 13). The API never reimplements service logic: handlers
validate input, enforce the caller's ownership, call the service, and
serialize safe results. There are no WebSocket application endpoints
here and no frontend assets.

## Configure and start

```bash
pip install -e ".[test]"
```

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `NEXUS_API_TOKEN` | yes (private routes) | — | Bearer token gating every private endpoint |
| `NEXUS_API_OWNER_USER_ID` | yes (private routes) | — | UUID of the single user the token acts as |
| `NEXUS_API_HOST` | no | `127.0.0.1` | Bind address (loopback by default) |
| `NEXUS_API_PORT` | no | `8000` | Bind port |
| `NEXUS_API_ENABLE_GATEWAY_IO` | no | `false` | Start gateway client, dispatcher, receiver, worker loop |
| `NEXUS_API_GATEWAY_URL` | no | `ws://127.0.0.1:8765` | Gateway WebSocket URL |
| `NEXUS_API_MCP_CONFIG_PATH` | no | — | MCP server config file (runtime is tool-free when unset) |
| `NEXUS_API_IDENTITY_KEYS_PATH` | no | — | JSON `{agent_id: base64-key}` for request signing and execution |
| `NEXUS_DATABASE_URL` / `NEXUS_DB_*` | deployment | local defaults | PostgreSQL (see persistence docs) |
| `NEXUS_LLM_*` | for chat/execution | — | Model config (`NEXUS_LLM_MODEL`, timeouts, limits) |

```bash
python -m backend.api --host 127.0.0.1 --port 8000
```

Without `NEXUS_API_TOKEN` + `NEXUS_API_OWNER_USER_ID`, every
private route answers `503 AUTH_NOT_CONFIGURED` (fail closed);
liveness stays open. Without `NEXUS_LLM_MODEL`, model-backed routes
answer `503 MODEL_NOT_CONFIGURED` while metadata routes keep working.
Without identity keys, signing and execution answer `503`.

## Authentication and authorization

Single-owner bearer tokens: `Authorization: Bearer <token>`,
compared in constant time, never logged or echoed. The token acts as
exactly one `owner_user_id`; every operation resolves ownership
against durable rows:

- Agents: listed/shown only when `agents.owner_user_id` matches.
- Conversations: the conversation's agent must be owned (derived
  server-side from the conversation row, never from caller input).
- Requests: the sender or the recipient side must be owned; approve,
  reject, and execute additionally require the *recipient* side.
- Unknown and unowned resources are indistinguishable (`404`), so
  existence never leaks across ownership boundaries.

Approval and execution decisions use the persisted recipient agent
id only. A caller-supplied `approved: true`, sender id, or owner id
is never proof of permission.

## Route inventory

Interactive schema: `GET /api/docs` (Swagger UI), raw OpenAPI:
`GET /api/openapi.json`. All routes below live under `/api/v1`.

| Method & path | Purpose |
|---|---|
| `GET /health/live` | Liveness, no auth |
| `GET /health/ready` | Database reachability (`200 ready` / `503 not-ready`, no detail) |
| `GET /agents` | Caller's local agents (id, active, public identity) |
| `GET /agents/{id}/peers` | Peerings of one owned agent |
| `GET /conversations?agent_id=` | Threads of one owned agent (bounded `limit`/`offset`) |
| `POST /conversations` | Open a thread (`agent_id`, optional `title`) → 201 |
| `GET /conversations/{id}/messages` | Stored turns (`user`/`agent` roles) |
| `POST /conversations/{id}/turns` | One model turn, persisted atomically |
| `POST /requests` | Create + queue a remote request → 201 (`request_id` accepted verbatim; reuse conflicts with 409 — not an idempotency key) |
| `GET /requests?role=inbound\|outbound` | Bounded list, optional `status`, `limit`/`offset` |
| `GET /requests/{id}` | Status, parties, and the stored answer once terminal |
| `GET /approvals/pending` | Inbound `WAITING_APPROVAL` requests for owned agents |
| `POST /requests/{id}/approve` | Approve as the persisted recipient (`reason` optional) |
| `POST /requests/{id}/reject` | Reject as the persisted recipient |
| `POST /requests/{request_id}/execute` | Run the Component 10 executor after its own re-verification; reports `COMPLETED`, `FAILED`, or live-claim `IN_PROGRESS` |
| `POST /events/ticket` | Spend REST auth for one single-use WebSocket ticket (+ expiry) |
| `WS /events/ws` | Authenticated event stream (`Sec-WebSocket-Protocol: nexus-ticket.<ticket>` or `?ticket=`; see `docs/WEBSOCKET_EVENTS.md`) |

Chat turns have no idempotency: each `POST` runs one turn. Remote
`request_id` values are caller-supplied identifiers, not retry keys.

## Status and error semantics

Success: `200` (queries, actions), `201` (created threads, requests).
Safe errors share one envelope: `{"code": "...", "message": "..."}`.

| Status | Meaning |
|---|---|
| 401 | Missing or wrong bearer token |
| 403 | Caller does not own the acting side |
| 404 | Unknown (or unowned) agent, conversation, or request |
| 409 | Duplicate id, illegal transition, unapproved execution |
| 422 | Invalid body, fields, pagination, or foreign correlation |
| 502 | Model returned no usable output |
| 503 | Not configured (`AUTH_…`, `MODEL_…`, `EXECUTION_…`, `SIGNING_…`) or transient upstream failure |
| 500 | Unexpected internal failure, no detail |

Framework validation failures use the same envelope
(`INVALID_INPUT`). Messages never contain exception text, queries,
keys, signatures, system instructions, or traces.

## Frontend contract (preview)

The Component 14 frontend should: store the token outside the repo
(env-authenticated backend proxy or OS keychain — never committed);
call `GET /health/ready` before first render; list agents, then
conversations; poll request/approval endpoints on an interval for
updates. Live push is intentionally deferred to Component 13: this
API offers no streaming, no subscriptions, and no push callbacks.

## Limitations

- Single-owner bearer auth only; no multi-user accounts, roles, or
  rotation API (rotate by replacing configuration and restarting).
- No CORS origins are enabled by default; browser frontends should
  use a same-origin backend proxy until Component 14 decides policy.
- Gateway live I/O (dispatcher consumer, receiver, worker loop) runs
  in-process only when explicitly enabled; otherwise responses queue
  in the outbox until an external worker runs.
- Model-less deployments serve metadata but not chat or execution.
- File-based identity keys are dev-grade: restrict file permissions;
  a key-store backend is future work.
