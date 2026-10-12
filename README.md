# Nexus V2

Network for independently owned AI agents: signed protocol, Ed25519 identity, gateway relay, PostgreSQL persistence, approval-gated remote execution, versioned REST API, authenticated WebSocket events, and a React frontend.

Each deployment is single-owner: one bearer token bound to one owner UUID. Two independent deployments talk to each other only through a shared Gateway, with every cross-agent message signed, verified, persisted, and explicitly approved before it runs.

> Scope note: the Gateway server itself (`gateway-v2`) lives in a **separate repository** and is not in this checkout. This repo contains the Nexus application (backend + frontend), migrations, and docs. The canonical two-machine walkthrough is `docs/A2A_DEMO.md`.

## Table of contents

- [What it does](#what-it-does)
- [Key features and status](#key-features-and-status)
- [Technology stack](#technology-stack)
- [Architecture](#architecture)
- [Repository structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Database and migrations](#database-and-migrations)
- [Run locally](#run-locally)
- [Build, test, and dev commands](#build-test-and-dev-commands)
- [API reference](#api-reference)
- [Core workflows](#core-workflows)
- [Authentication and security](#authentication-and-security)
- [Two-instance A2A demo (summary)](#two-instance-a2a-demo-summary)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## What it does

Nexus V2 lets an operator run a personal agent network:

1. **Chat locally** with their own agent (LangGraph runtime over durable Postgres conversation history, optional MCP tools).
2. **Send a signed remote task** (`REMOTE_QUERY`) to another operator's agent via the Gateway relay.
3. The recipient sees it in a **pending-approval queue**, approves or rejects it, and optionally **executes** it on their own model.
4. The signed result (`REMOTE_RESPONSE`) travels back through the Gateway and is **verified against the stored responder key** before the request is marked `COMPLETED`/`FAILED`.
5. A **React UI** (chat, requests, approvals, agents/pairing, provider settings) drives all of the above over `POST /requests`, `POST /requests/{id}/approve|reject|execute`, plus a live event stream that only triggers REST refetches.

Everything durable lives in PostgreSQL (13 tables, Alembic-managed). The Gateway is treated as untrusted transport: handoff (`DELIVERED`/`BUFFERED`) is not proof of execution, and signatures, expiry, replay, peer status, and approvals are re-checked at each step.

## Key features and status

Legend: **Implemented** = in code and supported by config/tests. **Partial** = works with noted limits. **Unverified** = could not be safely exercised here.

| Feature | Status | Where |
|---|---|---|
| Signed wire protocol (v1.0, 7 message types, canonical JSON) | **Implemented** | `backend/protocol/`, `docs/PROTOCOL.md`, `tests/protocol/` (64 tests) |
| Ed25519 identity (key gen, `agent_<sha256(pub)>`, sign/verify, in-mem replay) | **Implemented** | `backend/identity/`, `docs/IDENTITY.md`, `tests/identity/` (55 tests, 148 passed with protocol on Python 3.12.10) |
| PostgreSQL persistence (agents, peers, requests, messages, outbox, approvals, conversations, audit, provider creds, pairing) | **Implemented** | `backend/persistence/`, `alembic/versions/` (4 revisions), `docs/PERSISTENCE.md` |
| Request lifecycle (`QUEUED → SENT → … → WAITING_APPROVAL → APPROVED → PROCESSING → COMPLETED`, terminal `REJECTED/EXPIRED/FAILED/CANCELLED`) | **Implemented** | `backend/requests/`, `docs/REQUEST_SERVICE.md` |
| Outbox + Gateway client (claim with `SKIP LOCKED`, leases, correlated ACKs, 1 MiB send cap, graded `SENT/RETRYABLE/PERMANENT/UNCERTAIN`) | **Implemented** | `backend/worker/`, `backend/gateway_client/`, `docs/WORKER.md` |
| Inbound `REMOTE_QUERY` registration (verify vs stored key + `connected` peer, idempotent, `WAITING_APPROVAL`) | **Implemented** | `backend/inbound/`, `docs/INBOUND.md` |
| Approved remote execution (re-verifies approval/peer/expiry/key, 10 s expiry margin, atomic signed response + outbox) | **Implemented** | `backend/execution/`, `docs/REMOTE_EXECUTION.md` |
| Originating-side response reception | **Implemented** | `backend/responses/`, `tests/responses/` |
| Versioned REST API at `/api/v1` (8 routers, `/api/docs`, `/api/openapi.json`) | **Implemented** | `backend/api/`, `docs/REST_API.md` |
| Authenticated WebSocket events (single-use ticket + `nexus-events` subprotocol, 100-deep hub, no replay) | **Implemented** | `backend/api/events.py`, `backend/api/routes/events.py`, `docs/WEBSOCKET_EVENTS.md` |
| Local agent runtime (LangGraph `generate_response` graph, history-bounded, timeout-guarded, deterministic dev model) | **Implemented** | `backend/agent/`, `docs/AGENT_RUNTIME.md` |
| MCP tools (STDIO + Streamable-HTTP, verbatim allowlist, `env:VAR` secrets, single/multi-server) | **Implemented** | `backend/mcp/`, `config/mcp_servers.example.json`, `docs/MCP.md` |
| Secure pairing (one-time `inv.*` invitation, TTL ≤ 30 d, burn-on-redeem, `APPROVAL_RESPONSE` completion) | **Implemented** | `backend/pairing/`, `tests/pairing/` |
| Owner-scoped provider credentials (Fernet-encrypted, only `google_genai` verified in code) | **Partial** | `backend/provider_config/` — `SUPPORTED_PROVIDERS = {"google_genai": "Google Gemini"}`; `model_factory` can `init_chat_model` other providers if their `langchain-*` package is installed, but only the Gemini path is asserted in this repo |
| React frontend (chat, remote requests, approvals + approve-and-run, agents/network, AI-provider settings, live events) | **Implemented** | `frontend/`, `frontend/README.md` (unit tests mocked; E2E needs live backend + PG) |
| Two-machine A2A demo (hosted Gateway + deterministic or Gemini model) | **Implemented** (procedure, not automation) | `docs/A2A_DEMO.md`, `tests/a2a/` (deterministic-model proof; hosted-gateway test skips unless `NEXUS_TEST_GATEWAY_URL` is set) |
| Gateway server | **External** — separate `gateway-v2` repo, not in this checkout | `docs/A2A_DEMO.md` §1 |
| CI workflow | **Not present** — no `.github/`, no `*.yaml` CI found | — |
| Multi-user accounts, token streaming, cross-worker event fan-out | **Not implemented** (single-owner by design) | `backend/api/settings.py`, `frontend/README.md` known limits |

## Technology stack

Backend (`pyproject.toml`, `nexus-v2 0.8.0`, `requires-python >=3.10`):

| Layer | Packages (pinned) |
|---|---|
| Identity + credential crypto | `cryptography==46.0.5` (Ed25519 + Fernet) |
| Gateway transport | `websockets==16.1.1` |
| Persistence | `SQLAlchemy==2.0.35`, `asyncpg==0.29.0`, `alembic==1.13.0`, PostgreSQL 16 (`docker-compose.yml`, `postgres:16-alpine`) |
| Agent runtime | `langgraph==1.2.12`, `langchain==1.4.4`, `langchain-core==1.6.9` |
| MCP | `langchain[mcp]==1.4.4`, `fastmcp==4.1.0` |
| REST API | `fastapi==0.143.0`, `uvicorn==0.54.0` |
| Tests | `pytest>=8`, `pytest-asyncio==0.24.0`, `httpx==0.27.2` (`pip install -e ".[test]"`) |
| Optional providers | e.g. `langchain-google-genai` for the verified Gemini path; other `langchain-*` provider packages per `NEXUS_LLM_MODEL=provider:model-id` (unverified here) |

Frontend (`frontend/package.json`, `nexus-frontend 0.1.0`):

- `react ^19.2.0`, `react-dom ^19.2.0`, `react-router-dom ^7.9.4`, `react-markdown ^10.1.0`, `lucide-react ^0.550.0`
- `vite ^7.1.12`, `typescript ^5.9.3`, `tailwindcss ^4.1.14`, `@tailwindcss/vite`, `@vitejs/plugin-react ^5.0.4`
- Test: `vitest ^3.2.4`, `jsdom`, `@testing-library/*`, `playwright ^1.55.1` + `@axe-core/playwright` for E2E/a11y

Infra/config:

- `docker-compose.yml`: single `postgres:16-alpine` service (`nexus_v2_postgres`), defaults `nexus/nexus @ 127.0.0.1:5432`, volume `nexus_v2_pgdata`
- `alembic.ini` + `alembic/env.py` (async, `target_metadata = backend.persistence.models.Base.metadata`)
- `config/mcp_servers.example.json`: STDIO + Streamable-HTTP example, `allowedTools` allowlist, no secrets

## Architecture

### Runtime map

```mermaid
flowchart TB
  subgraph Browser["Browser (React 19 + Vite :5173)"]
    UI["Chat / Requests / Approvals / Agents / Settings"]
    ES["useEventStream (ticket + nexus-events WS)"]
  end

  subgraph Proxy["Vite dev proxy (/api -> :8000)"]
    PX["HTTP + WS proxy, changeOrigin:false"]
  end

  subgraph API["FastAPI (python -m backend.api, :8000, /api/v1)"]
    RT["Routers: health, agents, conversations, requests, events, provider, pairings, peers"]
    AUTH["deps.authenticate (single-owner Bearer)"]
    CS["ConversationService"]
    RS["RequestService"]
    IP["InboundProcessor + Dispatcher"]
    RR["ResponseReceiver"]
    EX["RemoteExecutor"]
    PS["PairingService + ProviderConfigService"]
    HUB["EventHub + TicketStore"]
  end

  subgraph Local["Local runtime"]
    AGR["AgentRuntime (LangGraph)"]
    MCP["MCPManager (STDIO / Streamable-HTTP)"]
    LLM["Provider model (Gemini verified / deterministic dev)"]
  end

  subgraph Store["PostgreSQL 16"]
    PG[("users, agents, agent_peers, remote_requests, messages, outbox_messages, approvals, conversations, conversation_messages, audit_events, provider_credentials, pair_invitations, pair_links")]
  end

  subgraph Relay["Gateway (separate gateway-v2 repo)"]
    GW[("WS relay: HANDSHAKE/WELCOME, MESSAGE/ACK, DELIVER (in-memory, restart loses buffered mail)")]
  end

  UI --> PX --> RT --> AUTH
  RT --> CS --> AGR --> LLM
  AGR --> MCP
  RT --> RS
  RS --> Store
  CS --> Store

  WORKER["OutboxDispatcher (claim/send/grade)"] --> GWC["GatewayClient per sender"]
  RS -.->|"creates QUEUED + outbox PENDING"| WORKER
  GWC <--> GW
  GW -->|"DELIVER REMOTE_QUERY"| IP --> Store
  GW -->|"DELIVER REMOTE_RESPONSE"| RR --> RS
  IP -.->|"WAITING_APPROVAL"| HUB --> ES
  CS -.->|"turn.completed"| HUB
  RS -.->|"created/updated/response"| HUB
  EX --> AGR
  EX --> Store
  PS --> Store
  ES -.->|"REST refetch (events are hints, never canonical)"| RT
```

What the diagram omits on purpose: there is no shared database between operators, no cross-worker event bus (hub is in-process), no presence/connect-disconnect tracking (`agent_peers.status` is logical pairing, not liveness).

### Startup (`python -m backend.api`)

`backend/api/__main__.py` reads `ApiSettings.from_env()` and runs `uvicorn("backend.api.app:app", host, port)` (loopback default, never `0.0.0.0` implicitly). `create_app()` mounts the 8 routers at `/api/v1`, docs at `/api/docs`, spec at `/api/openapi.json`, and installs 4 error handlers (`ApiError → status`, validation → `422 INVALID_INPUT`, Starlette HTTP → `404 NOT_FOUND`, fallback → `500 INTERNAL_ERROR`).

Lifespan (`build_production_state`) opens, in order: engine → session factory → ticket store → connectivity check → optional MCP manager → runtime (`AgentRuntimeConfig.from_env()`, `None` + warn when no model) → file identity provider (fail-closed) → `ConversationService` → Gateway I/O **only if** `NEXUS_API_ENABLE_GATEWAY_IO` is set (per-agent `GatewayClient.connect()`, `InboundProcessor`/`Dispatcher`, `ResponseReceiver.attach`, `PairingService.attach`, `OutboxDispatcher.run_forever()`). Shutdown closes everything in reverse; any startup failure disposes what was already opened.

### Request path (typical)

- **Local turn:** `POST /conversations/{id}/turns` → `authenticate` → ownership check → `effective_runtime(owner)` (saved DB creds win over startup runtime) → `ConversationService.submit_turn` (load agent+conversation+history → run model → atomically persist user+agent messages) → `conversation.turn.completed`.
- **Outbound remote:** `POST /requests` → `RequestService.create_remote_request` (validate → peer `connected`? → build + sign `REMOTE_QUERY` → atomic `QUEUED + message + outbox PENDING`) → worker claims (`SKIP LOCKED`) → verifies → `GatewayClient.send_message` → ACK `DELIVERED`/`BUFFERED` → `QUEUED → SENT`.
- **Inbound:** Gateway `DELIVER(REMOTE_QUERY)` → `InboundDispatcher` → `InboundProcessor.process` (recipient is me → sender known + `connected` (or `nexus.pair` bootstrap burning an invitation) → verify vs stored key → atomic `WAITING_APPROVAL + message + audit`) → `remote.request.approval_required`.
- **Approve + execute:** recipient-only `POST /requests/{id}/approve` (`WAITING_APPROVAL → APPROVED`) → `POST /requests/{id}/execute` (claims `APPROVED → PROCESSING`, re-verifies everything, runs `runtime.run(query, history=None)` so remote caps never widen tools, atomically stores signed `REMOTE_RESPONSE + outbox + COMPLETED/FAILED`) → worker delivers the answer.
- **Answer home:** `DELIVER(REMOTE_RESPONSE)` → `ResponseReceiver` → `RequestService.handle_remote_response` (parties mirror, state is `SENT`/`APPROVED`/`PROCESSING`, verify vs stored responder key) → `COMPLETED`/`FAILED` → `remote.response.received`.

## Repository structure

```text
.
├── backend/                  # FastAPI app + all domain components (~109 .py files, 14 packages)
│   ├── api/                  # Component 12: app.py (composition root), __main__.py, settings.py,
│   │   └── routes/           #   deps.py, schemas.py, errors.py, events.py (hub+tickets),
│   │                         #   health/agents/conversations/requests/events/provider/pairings/peers
│   ├── protocol/             # Component 1: envelope + 7 payloads, validation, strict JSON, canonicalization
│   ├── identity/             # Component 2: Ed25519 keys, agent IDs, sign/verify, replay registry
│   ├── persistence/          # Component 4: engine/session, models (13 tables), repositories
│   ├── requests/             # Component 5: lifecycle state machine + service (no network)
│   ├── gateway_client/       # Component 3/6: WS transport (HANDSHAKE/WELCOME, MESSAGE/ACK/DELIVER/ERROR)
│   ├── worker/               # Component 6: outbox dispatcher (claim → send → grade, backoff 1 s → 300 s)
│   ├── agent/                # Component 7: LangGraph runtime, model_factory, conversation_service, deterministic dev model
│   ├── mcp/                  # Component 8: MCPManager (STDIO + Streamable-HTTP, allowlist)
│   ├── inbound/              # Component 9: REMOTE_QUERY processor + dispatcher
│   ├── execution/            # Component 10: approved-request executor
│   ├── responses/            # Component 11: originating-side REMOTE_RESPONSE receiver
│   ├── pairing/              # invitation envelope, store (burn-on-redeem), service
│   └── provider_config/      # owner-scoped Gemini creds (Fernet), candidate/live test
├── frontend/                 # Component 14: React + TS + Vite chat/agent-network UI (see frontend/README.md)
│   ├── src/app/              # AuthGate, EventBus, router, Shell
│   ├── src/features/         # chat, requests, approvals, agents, settings
│   ├── src/lib/api/          # client.ts, endpoints.ts (mirrors /api/v1), types.ts
│   ├── src/lib/websocket/    # useEventStream.ts (ticket subprotocol, backoff, REST resync)
│   └── e2e/                  # Playwright specs + seed.py (needs live backend + PG)
├── alembic/                  # env.py (async) + versions/: c4init0001 → c50002 → c60003 → c70004
├── config/
│   └── mcp_servers.example.json  # operator MCP example (no secrets, no prod endpoints)
├── docs/                     # 12 component guides; start with docs/A2A_DEMO.md
│   ├── A2A_DEMO.md           # two-machine checklist (Render gateway, pairing, diagnostics)
│   ├── PROTOCOL.md / IDENTITY.md / PERSISTENCE.md / REQUEST_SERVICE.md / WORKER.md
│   ├── INBOUND.md / REMOTE_EXECUTION.md / MCP.md / AGENT_RUNTIME.md
│   └── REST_API.md / WEBSOCKET_EVENTS.md
├── tests/                    # 96 backend test files (pytest); 9 vitest suites + 5 Playwright specs in frontend/
├── docker-compose.yml        # postgres:16-alpine only
├── alembic.ini               # local-dev URL default (env overrides win)
├── pyproject.toml            # nexus-v2 0.8.0
└── .env.example              # copy to .env; dev defaults only, never secrets
```

`gateway-v2` is intentionally absent here — it is deployed separately (see the A2A summary below).

## Prerequisites

- **Python:** `>=3.10` per `pyproject.toml` (verified here on `3.12.10`).
- **PostgreSQL:** 16 reachable at `127.0.0.1:5432` by default. The repo ships only the database container; the API and frontend run on the host.
- **Docker Compose** for the one-command database (`docker compose up -d`).
- **Node.js 22+ and npm 10+** for the frontend (per `frontend/README.md`).
- **Git checkout** of this repo on `main` (`b73177e` at time of writing; history: first commit → Components 10–14 → two A2A PRs).
- **No Gateway checkout needed** for local chat/persistence work; cross-instance work needs the hosted `gateway-v2` URL.

## Installation

```bash
# 1. Clone and enter
git clone https://github.com/Pavanboga07/Nexus-V2.git
cd Nexus-V2

# 2. Python deps (backend + test extras)
pip install -e ".[test]"

# 3. Database container
docker compose up -d

# 4. Environment file
cp .env.example .env
# edit .env only for local DB tweaks; auth/model keys are exported per-shell (see Configuration)

# 5. Migrate to head (c4init0001 → c70004)
python -m alembic upgrade head

# 6. Frontend deps (separate terminal when needed)
cd frontend && npm install
```

> `pip show nexus-v2` will report the installed `0.8.0` distribution after step 2. `frontend/node_modules/` is git-ignored; `npm install` is required before any `npm run …` command.

## Configuration

Copy `.env.example` to `.env` for database/file knobs. **Never commit secrets.** The table below is derived from `backend/api/settings.py`, `backend/agent/config.py`, `backend/persistence/database.py`, `backend/mcp/config.py`, `alembic/env.py`, `docker-compose.yml`, and `frontend/vite.config.ts`. Values shown are safe defaults/placeholders.

### Backend API (`NEXUS_API_*`, read in `backend/api/settings.py`)

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `NEXUS_API_TOKEN` | Yes for every private route | — | Bearer token; compared with `secrets.compare_digest`, never logged. Example placeholder: `<long-random-string>` |
| `NEXUS_API_OWNER_USER_ID` | Yes with token | — | UUID the token acts as. Else all private routes return `503 AUTH_NOT_CONFIGURED` |
| `NEXUS_API_HOST` | No | `127.0.0.1` | Bind host; never defaults to `0.0.0.0` |
| `NEXUS_API_PORT` | No | `8000` | Bind port (1–65535) |
| `NEXUS_API_ENABLE_GATEWAY_IO` | No | `false` (`1/true/yes/on` enable) | Starts Gateway clients + dispatchers + worker loop. Leave off for local-only chat |
| `NEXUS_API_GATEWAY_URL` | No (yes for A2A) | `ws://127.0.0.1:8765` | Gateway WS URL; hosted demo uses `wss://<gateway-host>` |
| `NEXUS_API_IDENTITY_KEYS_PATH` | Yes for `POST /requests` and `…/execute` | — | JSON file `{"<agent_id>": "<base64-private-key-bytes>"}` (mode `0600`). Missing → `SIGNING_NOT_CONFIGURED` |
| `NEXUS_API_MCP_CONFIG_PATH` | No | — | MCP operator file (see `config/mcp_servers.example.json`). Unset → runtime stays tool-free |
| `NEXUS_API_CREDENTIAL_KEY` | Yes for provider save | — | Fernet urlsafe-b64 master key for `provider_credentials.key_encrypted`. Absent → saves fail closed |
| `NEXUS_API_ALLOWED_ORIGINS` | No | empty (same-origin + origin-less only) | Comma list of extra browser origins for `/events/ws`; never `*` |
| `NEXUS_API_TICKET_TTL_S` | No | `60.0` | Single-use WS ticket lifetime (seconds, must be positive) |

### Database (read in `backend/persistence/database.py`, `alembic/env.py`)

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `NEXUS_DATABASE_URL` | No (takes precedence when set) | — | Full async URL, e.g. `postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus` |
| `NEXUS_DB_HOST` / `NEXUS_DB_PORT` / `NEXUS_DB_USER` / `NEXUS_DB_PASSWORD` / `NEXUS_DB_NAME` | No | `127.0.0.1` / `5432` / `nexus` / `nexus` / `nexus` | Parts used when `NEXUS_DATABASE_URL` is unset; must match `docker-compose.yml` (`${NEXUS_DB_*:-…}`) |
| `NEXUS_TEST_DATABASE_URL` | No (tests only) | `…/nexus_test` | Backend test database, auto-created if missing |
| `ALEMBIC_DATABASE_URL` | No | — | Alembic override. Precedence: `alembic -x url=…` > `ALEMBIC_DATABASE_URL` > `NEXUS_DATABASE_URL` > `alembic.ini` |

### Agent runtime (`NEXUS_LLM_*`, read in `backend/agent/config.py`)

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `NEXUS_LLM_MODEL` | Yes for turns/execution | — | `provider:model-id` (e.g. `openai:gpt-4o`) or dev-only `deterministic:<label>` |
| `NEXUS_LLM_ALLOW_DETERMINISTIC` | Yes for `deterministic:*` | unset (fail-closed) | Must be set to use the fake model; production without creds fails loudly instead |
| `NEXUS_LLM_TEMPERATURE` | No | — | 0.0–2.0 |
| `NEXUS_LLM_MAX_TOKENS` | No | — | Positive int |
| `NEXUS_LLM_TIMEOUT_S` | No | `60.0` | Per-model-call timeout |
| `NEXUS_LLM_MAX_INPUT_CHARS` | No | `65536` | Input guard |
| `NEXUS_LLM_MAX_HISTORY` | No | `20` | Tail of Postgres history sent per turn |
| `NEXUS_LLM_SYSTEM_INSTRUCTION` | No | `You are Nexus, a helpful AI assistant. …` | System prompt (`DEFAULT_SYSTEM_INSTRUCTION`) |
| Provider keys (e.g. `GOOGLE_API_KEY`) | Yes for live models | — | Read by the provider SDK itself, never by `AgentRuntimeConfig`; keep out of this repo |

`NEXUS_MCP_CONFIG_PATH` is the non-`API` alias read by `MCPManagerConfig.from_env()`; in production `app.py` passes `ApiSettings.mcp_config_path` to `MCPManagerConfig.from_file()`, so set the `NEXUS_API_*` form for the server. `NEXUS_RUN_LLM_SMOKE=1` enables the optional live-model smoke test only.

### Frontend (read in code, not `VITE_*`)

| Variable | Where | Purpose |
|---|---|---|
| `NEXUS_DEV_API` | `frontend/vite.config.ts` (`process.env.NEXUS_DEV_API ?? http://127.0.0.1:8000`) | Dev/preview `/api` proxy target; e.g. two backends side-by-side in the A2A demo |
| `NEXUS_E2E_TOKEN` | `frontend/playwright.config.ts` | Playwright bearer (default `e2e-frontend-token`) |
| `NEXUS_DATABASE_URL` + `E2E_OWNER_USER_ID` | `frontend/e2e/seed.py` | E2E seed DB (`…/nexus_frontend_e2e`) and owner (`E2E_OWNER_USER_ID` required) |

The UI deliberately uses **no `VITE_*` variables**: the token lives in tab-scoped `sessionStorage` (`nexus.api.token`), base URL in `localStorage` (`nexus.api.baseUrl`), chat agent in `localStorage` (`nexus.chat.agentId`).

## Database and migrations

- Engine: async SQLAlchemy + `asyncpg`, `pool_pre_ping`, `expire_on_commit=False`, short `session_scope()` transactions (commit-or-rollback), `SELECT … FOR UPDATE` + `SKIP LOCKED` for outbox/request claims.
- 13 tables: `users`, `agents` (no secret column — asserted in tests), `agent_peers` (directed, unique, no-self), `remote_requests` (+ `conversation_id`, no FK), `messages` (wire minus `signature` in `envelope` JSONB + `signature`), `outbox_messages` (`PENDING/PROCESSING/SENT/FAILED/UNCERTAIN` + claim token/lease), `approvals` (one row per request), `conversations`, `conversation_messages` (`user/agent/system`), `audit_events` (append-only facts), `provider_credentials` (PK `owner_user_id`, Fernet blob only), `pair_invitations` (hash, never secret), `pair_links`.
- Revisions (`alembic/versions/`): `c4init0001_initial_schema` (9 tables) → `c50002_outbox` (+ `outbox_messages`, `remote_requests.conversation_id`) → `c60003_outbox_leases` (+ claim token/lease/`UNCERTAIN`) → `c70004_provider_pairing` (+ `provider_credentials`, `pair_invitations`, `pair_links`).

```bash
docker compose up -d
python -m alembic upgrade head
python -m alembic history        # verify the 4-step chain
python -m alembic downgrade -1  # safe to inspect; re-upgrade after
```

`alembic.ini`'s `sqlalchemy.url` is only the local-dev fallback; the `-x url` / `ALEMBIC_DATABASE_URL` / `NEXUS_DATABASE_URL` precedence above wins.

## Run locally

One-machine, Gateway I/O **off** (chat, persistence, approvals scaffolding, UI browsing):

```bash
docker compose up -d
python -m alembic upgrade head

# shell 1 — backend (set the two auth vars; loopback defaults shown)
$env:NEXUS_API_TOKEN="<long-random-string>"
$env:NEXUS_API_OWNER_USER_ID="<your-owner-uuid>"
python -m backend.api --host 127.0.0.1 --port 8000
# docs: http://127.0.0.1:8000/api/docs | spec: /api/openapi.json
# health: GET /api/v1/health/live (no auth), GET /api/v1/health/ready (SELECT 1)

# shell 2 — frontend
cd frontend
npm install
npm run dev   # http://127.0.0.1:5173, /api proxied to :8000
```

For remote requests/execution on one machine you additionally need, per backend: `NEXUS_API_IDENTITY_KEYS_PATH` (agent→key JSON), a model (`NEXUS_LLM_MODEL` + provider key, or `deterministic:<label>` + `NEXUS_LLM_ALLOW_DETERMINISTIC=1`), and `NEXUS_API_ENABLE_GATEWAY_IO=1` + `NEXUS_API_GATEWAY_URL` pointing at a running Gateway. Without them the API honestly returns `SIGNING_NOT_CONFIGURED` / `MODEL_NOT_CONFIGURED` / `EXECUTION_NOT_CONFIGURED` instead of faking success.

Two-backend A2A on one machine (deterministic): run two PG databases, two owners/tokens/ports (e.g. `:8000`/`:8002`), two frontends (`NEXUS_DEV_API=http://127.0.0.1:8002 npm run dev -- --port 5174` for the second), one local Gateway, then follow `docs/A2A_DEMO.md` §§2–6 with `deterministic:demo`.

## Build, test, and dev commands

All commands verified against `pyproject.toml`, `frontend/package.json`, `alembic.ini`, and `docs/`; outcomes below are what was actually observed in this environment (Python 3.12.10, no PG client tools, `frontend/node_modules/` absent).

| Command | What it does | Observed |
|---|---|---|
| `pip install -e ".[test]"` | Installs `nexus-v2 0.8.0` + `pytest/pytest-asyncio/httpx` | Required before `pytest`; `pip show nexus-v2` was absent pre-install here |
| `python -m pytest tests/protocol tests/identity -q` | No-DB contract + crypto suite | **148 passed in ~0.5 s** |
| `python -m pytest tests/ -q` | Full backend suite (96 files) | Needs live PG (`NEXUS_TEST_DATABASE_URL`, default `…/nexus_test`); **not run here** — no `pg_isready`/server was available, so DB-backed suites (persistence, worker, a2a, most of api) are **unverified in this pass** |
| `python -m pytest tests/a2a/ -q` | Deterministic two-instance proof (real sockets/signatures/workers, no hosted GW) | Needs PG + deps; documented in `docs/A2A_DEMO.md`, not run here |
| `python -m alembic upgrade head` | Applies `c4init0001 → c70004` | Needs PG; not run here (no server) |
| `python -m backend.api --host 127.0.0.1 --port 8000` | Serves the API (entry in `backend/api/__main__.py`) | Import check passed (`ApiSettings.from_env({})` → `auth_configured=False`, `127.0.0.1:8000`); live serve needs PG |
| `cd frontend && npm install` | Installs React/Vite/Playwright toolchain | Required; `node_modules/` absent here so `npm run …` below are **unverified in this pass** but match `package.json` verbatim |
| `npm run dev` | Vite `:5173`, `/api → NEXUS_DEV_API ?? :8000` (HTTP+WS, `changeOrigin:false`) | Per `vite.config.ts` + `frontend/README.md` |
| `npm run build` | `tsc --noEmit && vite build` → `dist/` | Same |
| `npm run preview` | `vite preview --port 5173` with the same `/api` proxy | Same |
| `npm test` / `npm run test:watch` | `vitest run` / watch (9 suites, jsdom, `e2e/` excluded) | Same |
| `npm run test:e2e` | `playwright test` vs preview + live backend (`nexus_frontend_e2e`, `e2e-frontend-token`, owner `11111111-…`) with `frontend/e2e/seed.py` | Same; asserts honest unconfigured-model states where creds are missing |
| `npm run typecheck` / `npm run lint` | Both are `tsc --noEmit` | Same (no separate ESLint config) |

No CI workflow exists to run any of the above automatically.

## API reference

Base prefix is `/api/v1` for all routers (`backend/api/app.py`); interactive docs at `/api/docs`, OpenAPI at `/api/openapi.json`. Error envelope is `{"code","message"}`. Paths below are the full paths including the prefix, verified against `backend/api/routes/*.py` and mirrored 1:1 in `frontend/src/lib/api/endpoints.ts`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/health/live` | No | Liveness (`ok`), no deps |
| `GET` | `/api/v1/health/ready` | No | `ready` after `SELECT 1`, else `503 not-ready` (no detail) |
| `GET` | `/api/v1/agents` | Bearer | Owned agents only |
| `GET` | `/api/v1/agents/{agent_id}/peers` | Bearer | Peerings of an owned agent |
| `GET` | `/api/v1/conversations?agent_id&limit&offset` | Bearer | Threads (`limit` 1–200, `offset` 0–1000) |
| `POST` | `/api/v1/conversations` | Bearer | `{agent_id, title?}` → `201` |
| `GET` | `/api/v1/conversations/{conversation_id}/messages?limit&offset` | Bearer | History slice |
| `POST` | `/api/v1/conversations/{conversation_id}/turns` | Bearer | `{message}` → `{answer, model}` |
| `POST` | `/api/v1/requests` | Bearer | `{sender, recipient, query, caps?, request_id?, message_id?, expires_in_s?}` → `201 QUEUED` |
| `GET` | `/api/v1/requests?role=outbound\|inbound&status?&limit&offset` | Bearer | Requests involving the caller |
| `GET` | `/api/v1/requests/{request_id}` | Bearer | Status + `answer` when terminal |
| `GET` | `/api/v1/approvals/pending` | Bearer | Inbound `WAITING_APPROVAL` |
| `POST` | `/api/v1/requests/{request_id}/approve` | Bearer | `{reason?}`, recipient-only |
| `POST` | `/api/v1/requests/{request_id}/reject` | Bearer | `{reason?}`, recipient-only |
| `POST` | `/api/v1/requests/{request_id}/execute` | Bearer | `APPROVED`/`PROCESSING` → `{status, executed, response_message_id}` |
| `POST` | `/api/v1/events/ticket` | Bearer | Single-use WS ticket (`expires_at`) |
| `WS` | `/api/v1/events/ws` | Ticket subprotocol | `Sec-WebSocket-Protocol: nexus-ticket.<ticket>, nexus-events`; server selects `nexus-events`; streams `{event_id, type, timestamp, data}` |
| `GET` | `/api/v1/settings/ai-provider` | Bearer | `{configured, provider?, model?, updated_at?}` (never the key) |
| `PUT` | `/api/v1/settings/ai-provider` | Bearer | `{provider, model, api_key}` (Fernet-encrypted; only `google_genai` accepted) |
| `DELETE` | `/api/v1/settings/ai-provider` | Bearer | `{removed: bool}` |
| `POST` | `/api/v1/settings/ai-provider/test` | Bearer | Saved-or-candidate live `OK` probe → `{ok, provider, model, latency_ms, reply_preview}` |
| `POST` | `/api/v1/pairings/invitations` | Bearer | `{agent_id, ttl_s?}` → `201 {invitation_id, invitation, secret, expires_at}` |
| `GET` | `/api/v1/pairings/invitations?agent_id` | Bearer | Invitations with `used/revoked` |
| `POST` | `/api/v1/pairings/invitations/{invitation_id}/revoke` | Bearer | `{revoked: bool}` |
| `POST` | `/api/v1/pairings/validate` | Bearer | `{invitation}` → `{agent_id, public_key, fingerprint, expires_at}` (crypto check only) |
| `POST` | `/api/v1/pairings/accept` | Bearer | `{invitation, sender_agent_id}` → `201` pending link |
| `GET` | `/api/v1/pairings?agent_id` | Bearer | Pair links |
| `POST` | `/api/v1/pairings/complete` | Bearer | `{request_id}` → `{pairing_id, approved, message_id, ack}` (sends `APPROVAL_RESPONSE` via Gateway) |
| `POST` | `/api/v1/peers/{peer_agent_id}/revoke` | Bearer | Revokes all owned peerings → `{revoked: int}` |

Event `type`s (both `backend/api/events.py` and `frontend/src/lib/api/types.ts`): `conversation.turn.completed`, `remote.request.created`, `remote.request.approval_required`, `remote.response.received`, `remote.request.updated`, `execution.updated`, `remote.pairing.updated`.

Minimal examples (placeholders, not real credentials):

```bash
BASE=http://127.0.0.1:8000
TOKEN="<long-random-string>"

curl -s $BASE/api/v1/health/ready
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/v1/agents
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":"<owned-agent-id>"}' $BASE/api/v1/conversations
```

## Core workflows

- **Pairing two operators (UI path):** B creates invitation (`POST /pairings/invitations`) → sends the `inv.…` string out-of-band → A validates (`/pairings/validate`) then accepts (`/pairings/accept`, creates remote-agent + `connected` peer + pairing-flagged request) → B approves (`/requests/{id}/approve`) → B completes (`/pairings/complete`, Gateway `APPROVAL_RESPONSE`) → A’s dispatcher records acceptance. Peer rows must come from this flow, never hand-inserted.
- **Remote task:** A `POST /requests` (`QUEUED`) → worker delivers via Gateway (`SENT`) → B’s inbound registers (`WAITING_APPROVAL`, `approval_required` event) → B approves → B executes (model runs with `history=None`, response signed and queued) → worker delivers → A’s receiver verifies and completes (`response.received`).
- **Provider setup:** Settings → AI Provider → `google_genai` + model (e.g. `gemini-3.5-flash`) + key → Save (Fernet) → Test (20 s live `OK` probe, key-redacted errors). Turns/executions resolve per-owner saved creds first, else the startup `NEXUS_LLM_*` runtime.
- **Live UI updates:** sign in (gate probes `GET /health/ready` + authed `GET /agents`) → `POST /events/ticket` → `WS /events/ws` with both subprotocols → per-scope REST refetch on each event, `event_id` dedup, fresh ticket + full REST resync on reconnect. Events are hints; REST is canonical.

## Authentication and security

- **REST (single-owner):** `Authorization: Bearer <NEXUS_API_TOKEN>` bound to `NEXUS_API_OWNER_USER_ID`. Constant-time compare, ASCII-only, never logged. Both unset → `503 AUTH_NOT_CONFIGURED`; wrong/missing → `401 UNAUTHORIZED`. Ownership is enforced against durable rows (`agents.owner_user_id`, conversation→agent, request sender/recipient); unknown and unowned IDs return identical `404`s to avoid oracle leaks. Health endpoints stay unauthenticated by design.
- **WebSocket:** ticket issued over Bearer auth, redeemed **once** via subprotocol `nexus-ticket.<ticket>` (never query string, never logged), owner must equal settings owner. Origin: absent (non-browser) or same-host allowed, else must exactly match `NEXUS_API_ALLOWED_ORIGINS` (no wildcards) or the handshake is refused pre-accept. Bad/expired/foreign/reused ticket → close `4401`; slow consumer (100-deep queue full) → `1013`; normal close → `1001`.
- **Message trust:** every cross-agent envelope is verified in a fixed order — structure → sender/key binding (stored `agents.public_key`) → expiry → replay (`message_id` registry, in-mem after durable commit) → Ed25519 signature over canonical bytes (`signature` excluded, sorted keys, no whitespace, UTF-8). `agent_peers.status == connected` (or a valid `nexus.pair` bootstrap burning an invitation) is required; `1 MiB` send cap and `4 MiB` frame cap bound abuse; provider keys are Fernet-encrypted at rest and never returned by the API; identity private keys live only in the `0600` JSON file and in memory.
- **What this is not:** no multi-user authz, no per-request sandboxing beyond the tool allowlist + input/history/tool-output truncation, no Gateway attestation (it sees envelopes but cannot forge signatures), file-key handling is dev-grade (rotate by replacing the file + restart).

## Two-instance A2A demo (summary)

Full checklist: `docs/A2A_DEMO.md`. Essence:

1. Deploy `gateway-v2` (branch `fix/gateway-render-compat`, `/healthz` check, `python -m gateway --host 0.0.0.0 --port $PORT`) and note the `wss://…` URL. Free-tier sleeps lose buffered mail — run start-to-finish in one session.
2. Per machine (isolated DBs, distinct owners/tokens/keys): migrate, create owner UUID + token + one owned agent + `NEXUS_API_IDENTITY_KEYS_PATH` JSON (`0600`).
3. Provider per machine: UI Settings → AI Provider (Gemini + `NEXUS_API_CREDENTIAL_KEY`) or `NEXUS_LLM_MODEL='deterministic:demo'` + `NEXUS_LLM_ALLOW_DETERMINISTIC=1`.
4. Both backends with `NEXUS_API_GATEWAY_URL=wss://…` + `NEXUS_API_ENABLE_GATEWAY_IO=1`; frontends via `NEXUS_DEV_API` overrides if sharing hardware.
5. Pair (B invite → A accept → B approve → B complete), then A `POST /requests` → B approve → B execute → A `COMPLETED`. Diagnostics for `QUEUED`-stuck / `SIGNING_NOT_CONFIGURED` / `409` / `503` / pairing-stuck / Render-sleep are in the guide’s table. Automated sibling: `pytest tests/a2a/ -q` (deterministic, no hosted GW).

## Known limitations

- Single-owner bearer auth only; no teams, roles, or per-agent tokens. Two API workers do not share WebSocket delivery — run one API process per owner or accept split delivery (REST stays canonical).
- Gateway is in-memory: `BUFFERED` mail and replay state are lost on restart; `DELIVERED`/`BUFFERED` ACKs mean handoff, never execution. `UNCERTAIN` outbox rows are never auto-retried and need operator attention.
- `PROCESSING` is sticky: a crash mid-execution surfaces as `IN_PROGRESS` on retry, not a re-run. `COMPLETED`/`FAILED` replays return `executed=False`.
- Execution runs the recipient model with `history=None` and the recipient’s allowlist tools only; remote `requested_capabilities` never widen tool access (narrower per-request subsets are future work per `docs/MCP.md`).
- Only `google_genai` is an accepted `provider` in `provider_config/service.py`; other `provider:model` strings need their `langchain-*` package plus code-level allowlisting. No token streaming endpoint; chat shows a spinner until the turn HTTP response arrives.
- `docs/A2A_DEMO.md` non-goals apply: LLM answer quality, two-machine networking inside tests, and production hardening (single-owner, in-proc hub, unbounded retry notes, no MCP cap) are explicitly out of scope.
- No `LICENSE` file and no CI workflow ship in this checkout — confirm reuse/verification policy before distributing or deploying beyond demos.

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| `503 AUTH_NOT_CONFIGURED` on private routes | `NEXUS_API_TOKEN` or `NEXUS_API_OWNER_USER_ID` unset/invalid UUID → export both and restart the API |
| `401 UNAUTHORIZED` | Wrong token or missing `Authorization: Bearer` header → re-paste token in UI Settings / `curl -H` |
| `404` for an ID you “know” | Ownership masking (unknown ≡ unowned) or wrong owner DB → verify owner UUID matches the seeded `users` row |
| `SIGNING_NOT_CONFIGURED` on `POST /requests` | `NEXUS_API_IDENTITY_KEYS_PATH` unset/unreadable or agent missing from the JSON → point at the `0600` file and restart |
| `MODEL_NOT_CONFIGURED` / `EXECUTION_NOT_CONFIGURED` on turns/execution | No `NEXUS_LLM_MODEL` and no saved provider creds (or no `NEXUS_API_CREDENTIAL_KEY`) → set provider via UI or env; deterministic needs `ALLOW_DETERMINISTIC=1` |
| Request stuck `QUEUED` | `NEXUS_API_ENABLE_GATEWAY_IO` off, `NEXUS_API_GATEWAY_URL` unreachable, or Gateway asleep → enable, check URL, wake the Render service |
| `409` on create/approve/execute | Caller-supplied `request_id`/`message_id` reuse, wrong state, or concurrent claim → fresh IDs; `GET /requests/{id}` for state; retry execute (replay is safe) |
| `WS` close `4401` | Ticket in query string, reused/expired/foreign ticket, or wrong subprotocols → use `POST /events/ticket` + `[nexus-ticket.<t>, nexus-events]`, fresh ticket per reconnect |
| `WS` close `1013` / missed events | Slow consumer (100-queue full) → reconnect (fresh ticket) and re-query REST; events are never replayed |
| `alembic upgrade head` points at the wrong DB | Precedence confusion → use `alembic -x url=<url> upgrade head` or set `ALEMBIC_DATABASE_URL` / `NEXUS_DATABASE_URL` |
| `npm run dev` proxies to the wrong backend | Stale `NEXUS_DEV_API` or cached `localStorage:nexus.api.baseUrl` → export/clear and restart Vite; keep `changeOrigin:false` |
| E2E seed complains about owner/keys | `E2E_OWNER_USER_ID` unset or `.keys.json` deleted → export the UUID, re-run `python frontend/e2e/seed.py`, restart backend with the new `NEXUS_API_IDENTITY_KEYS_PATH` |

Backend logs association: `nexus.api` (startup/shutdown + `close_state` warns), outbox/worker grading, and `ApiError` envelopes (`code/message`, no secret leakage) — check those before opening an issue.

## Contributing

No contributing guide or CI ships with this checkout (verified: no `.github/`, no `CONTRIBUTING.md`). Sensible defaults until one is added:

1. Create a feature branch from `main`; keep Gateway changes in `gateway-v2`.
2. Add/extend `tests/` alongside behavior (backend: `python -m pytest tests/ -q` against PG; frontend: `npm test` + `npm run typecheck`).
3. Update the matching `docs/*.md` page when you change a component contract.
4. Never commit `.env`, `*.pem`/`*.key`/`*.secret`, `frontend/e2e/.keys.json`, or provider keys (all git-ignored or never written to disk in plaintext here).

## License

No `LICENSE` (or `LICENCE`) file was found in this checkout at time of writing, and `frontend/package.json` declares `"license": "UNLICENSED"` (private). Treat the code as **all rights reserved** until the owner adds an explicit license. Do not republish or deploy beyond private evaluation without clarification from the repository owner.
