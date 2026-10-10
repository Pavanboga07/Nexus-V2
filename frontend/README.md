# Nexus Frontend (Component 14)

Chat-first workspace for the Nexus personal agent network: local assistant
chat, connected-agent remote requests, approvals with approve-and-run, agents,
settings, and an authenticated live-event stream.

## Requirements

- Node.js 22+, npm 10+
- The Nexus backend running locally (default `http://127.0.0.1:8000`), backed
  by PostgreSQL, with `NEXUS_API_TOKEN` and `NEXUS_API_OWNER_USER_ID` set.
- No language model is needed to browse agents, requests, or approvals, but
  chat turns and remote execution honestly report `MODEL_NOT_CONFIGURED` /
  `EXECUTION_NOT_CONFIGURED` until `NEXUS_LLM_MODEL` (plus provider
  credentials) is configured on the backend.

## Install and run

```bash
cd frontend
npm install
npm run dev        # Vite on http://127.0.0.1:5173, /api proxied to :8000
```

Production:

```bash
npm run build      # typecheck + static bundle in dist/
npm run preview    # serve dist/ on :5173 with the same /api proxy
```

## First run

Open the app and complete the connection gate: confirm the API base URL
(default is the same origin, which uses the dev/proxy path) and paste the
backend bearer token into the password field. `Connect` probes
`GET /health/ready` and an authenticated `GET /agents` before entering the
workspace. The token is kept in tab-scoped `sessionStorage` (never
`localStorage`, never a `VITE_*` variable, never committed) and can be
replaced or cleared in Settings. It is sent only as an
`Authorization: Bearer` header, never in a URL.

## Proxy and security contract

`/api` (REST and the `/api/v1/events/ws` WebSocket) is proxied same-origin,
so the browser never talks to the API host directly and the backend's
missing CORS surface plus its WebSocket origin policy keep working
unchanged. No backend CORS, auth, or origin setting was weakened for
development. The proxy forwards the original `Host`, preserving the
same-origin check semantics.

## Live events

After sign-in the app issues a single-use ticket
(`POST /api/v1/events/ticket`) and opens `/api/v1/events/ws` offering the
`nexus-ticket.<ticket>` protocol for authentication alongside the fixed
`nexus-events` protocol, which is the name the server selects in the
handshake (browsers fail the handshake when they offer protocols but the
server selects none; the ticket itself is never echoed back).
Query-string tickets are never used. Events (`conversation.turn.completed`,
`remote.request.created`, `remote.request.approval_required`,
`remote.response.received`, `remote.request.updated`, `execution.updated`)
only trigger REST refetches of the affected scope; payloads are never
treated as canonical. Duplicate `event_id`s are ignored, reconnects use
bounded exponential backoff with a fresh ticket, and every reconnect
resynchronizes from REST (events are not replayed).

## Tests

```bash
npm test          # vitest unit/integration suite
npm run test:e2e  # Playwright against preview + a live backend (see below)
```

Browser E2E expects the E2E backend on `:8000` with database
`nexus_frontend_e2e`, token `e2e-frontend-token`, and owner
`11111111-2222-4333-8444-555555555555`:

```bash
# from the repository root
$env:NEXUS_DATABASE_URL="postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_frontend_e2e"
$env:E2E_OWNER_USER_ID="11111111-2222-4333-8444-555555555555"
python frontend/e2e/seed.py   # migrate + seed agent, peer, pending approval
# (terminal 1) backend with NEXUS_API_TOKEN=e2e-frontend-token on :8000
# (terminal 2) cd frontend && npm run test:e2e
```

The seed keeps one stable local agent (its key is written once to
`frontend/e2e/.keys.json`, git-ignored) and refreshes request-scoped rows
on every run, so re-seeding before a suite run never requires a backend
restart. Pass the keys file to the backend once via
`NEXUS_API_IDENTITY_KEYS_PATH=frontend/e2e/.keys.json` — without it,
request creation honestly reports `SIGNING_NOT_CONFIGURED`. If the keys
file is deleted, the next seed generates a fresh agent and the backend
must be restarted to pick it up.

The suite covers the auth gate, chat flow, remote-request creation,
approve-and-run sequencing, the real subprotocol WebSocket handshake (plus
query-string rejection with close code 4401), axe accessibility scans, and
desktop/mobile overflow checks. Chat-turn success and live model execution
need provider credentials, so E2E asserts the honest unconfigured-model
states there; the success paths are covered by mocked unit tests.

## Known limitations

- Single-owner bearer auth only; no multi-user accounts.
- The event hub is in-process per API deployment; two API workers do not
  share delivery. REST is the source of truth after any disconnect.
- No token streaming endpoint exists; turns show a sending indicator until
  the HTTP response arrives.
- The backend exposes no agent connect/disconnect operation; the Agents
  page reports availability honestly instead of faking controls.
