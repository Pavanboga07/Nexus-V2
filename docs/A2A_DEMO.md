# Nexus A2A Demo Guide (two independent instances, one Gateway)

This is the practical checklist for the agent-to-agent demonstration:
User A (machine 1) sends a remote task to User B's agent (machine 2)
through a shared Gateway; User B approves and executes it; User A
receives the verified, persisted result. Only the Gateway needs a
public address — each Nexus backend (and frontend) runs locally on its
own machine with outbound access to it.

## 0. What you need

- PostgreSQL reachable from each Nexus machine (local instances are
  fine; the two backends must NOT share a database).
- Python 3.12 with the Nexus checkout and its dependencies installed.
- Node.js 22+ for the frontend (optional for the demo; the API alone
  suffices, the UI only displays it).
- A Render account for the Gateway (or any host that forwards a
  public port to the process).

## 1. Deploy the Gateway to Render

1. In the Render dashboard: New → Web Service → select the
   `gateway-v2` repository.
2. **Branch:** select `fix/gateway-render-compat` (it carries the
   `/healthz` endpoint; `main` predates it).
3. Build command:
   ```bash
   pip install "nexus-v2 @ git+https://github.com/Pavanboga07/Nexus-V2.git@573c1a82e831371c653afe9e7102bb8a437c040a" --no-deps && pip install -e .
   ```
4. Start command (bind `0.0.0.0`; the `127.0.0.1` default is
   unreachable from Render's proxy and the deploy will hang):
   ```bash
   python -m gateway --host 0.0.0.0 --port $PORT
   ```
5. Health Check Path: `/healthz` (the only HTTP surface; everything
   else on the port is WebSocket).
6. Note the public hostname, e.g.
   `https://nexus-gateway-xxxx.onrender.com`. Both Nexus backends use
   `wss://nexus-gateway-xxxx.onrender.com` (TLS terminates at Render;
   the Ed25519 handshake still runs end-to-end inside it).

Free-plan notes: the service sleeps after ~15 minutes idle and every
restart loses buffered mail and replay state (in-memory by design).
Run the demo start-to-finish in one session, or use a paid plan for
anything longer.

## 2. Prepare each Nexus machine (repeat per machine, distinct values)

```bash
# 1. Database (isolated per machine) + migrations
export NEXUS_DATABASE_URL="postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_a"
python -m alembic upgrade head   # from the Nexus-V2 checkout
```

Create the owner identity (do this once per machine; the two machines
must have DIFFERENT owners, tokens, and agent keys):

```bash
export NEXUS_API_OWNER_USER_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
export NEXUS_API_TOKEN="Machine-A operator token goes here (long random string)"
```

Create one local agent and its signing key, then register them. The
test suite's `seed_*` helpers show the exact repository calls; the
operational minimum is:

- one `users` row for the owner UUID,
- one `agents` row (owned, active) with the Ed25519 public key,
- `NEXUS_API_IDENTITY_KEYS_PATH` pointing at
  `{"<agent_id>": "<base64 private-key bytes>"}` (mode 0600),
- one `agent_peers` row toward the other machine's agent id with
  status `connected` (both directions, one row per machine),
- the other machine's `agents` row present (unowned) so signatures
  resolve.

## 3. Deterministic model (no paid LLM key needed)

On both machines, explicitly opt in to the development model:

```bash
export NEXUS_LLM_MODEL="deterministic:demo"
export NEXUS_LLM_ALLOW_DETERMINISTIC=1
```

Answers come back labeled `[deterministic:demo] ...` through the
normal runtime interface (request, approval, tools, signing,
transport, validation, and persistence are all real). Without these
two variables, model routes honestly report `MODEL_NOT_CONFIGURED`
and execution reports `EXECUTION_NOT_CONFIGURED` — never a silent
fake. Real provider references (`openai:…`, …) behave exactly as
before.

## 4. Start both backends (same Gateway URL, everything else distinct)

```bash
export NEXUS_API_GATEWAY_URL="wss://nexus-gateway-xxxx.onrender.com"
export NEXUS_API_ENABLE_GATEWAY_IO=1
export NEXUS_API_HOST=127.0.0.1 NEXUS_API_PORT=8000
python -m backend.api
```

Expected log: `no NEXUS_LLM_MODEL configured` must NOT appear (the
deterministic model is configured); each backend connects its
GatewayClient at startup (`_open_gateway_io`).

## 5. Run the demo flow

1. **A submits:** `POST /api/v1/requests`
   `{sender_agent_id: <A>, recipient_agent_id: <B>, query: "..."}` →
   `201`, status `QUEUED`. (Or use the frontend's collapsed
   "Ask a connected agent" panel.)
2. **Transport:** A's worker sends it; the Gateway routes it; B's
   dispatcher registers it. Confirm:
   `GET /api/v1/approvals/pending` on B lists it as
   `WAITING_APPROVAL`.
3. **B approves:** `POST /api/v1/requests/{id}/approve` → `APPROVED`.
4. **B executes:** `POST /api/v1/requests/{id}/execute` → `COMPLETED`
   with `executed: true` and a `response_message_id`. The answer is
   the deterministic text.
5. **A receives:** B's worker delivers the signed response; A's
   receiver validates and completes. Confirm on A:
   `GET /api/v1/requests/{id}` → `COMPLETED`,
   `response_message_id` matches B's, `answer` equals the
   deterministic text.
6. **Frontend (optional):** point each browser at its local
   frontend, paste that machine's bearer token, and watch Chats /
   Requests / Approvals update over the live event stream.

## 6. Diagnose failures

| Symptom | Check |
|---|---|
| Backend exits at startup with gateway I/O error | `NEXUS_API_IDENTITY_KEYS_PATH` set and readable; file is non-empty `{agent: key}` JSON |
| Request stuck `QUEUED` | Worker's GatewayClient connected? (`NEXUS_API_GATEWAY_URL` reachable; Render service awake, not sleeping) |
| `SIGNING_NOT_CONFIGURED` on create | Keys file missing/unset (create path) |
| Approval `409` | Item already decided or expired; re-query state first |
| Execute `503` | Model unconfigured: set both `NEXUS_LLM_*` vars (deterministic or provider) |
| Response never completes A | B's worker running; A still `SENT`; check B's request reached `COMPLETED` first |
| Render deploy unhealthy | Health Check Path must be `/healthz`; start command must bind `0.0.0.0 --port $PORT` |
| `HEAD /` tracebacks in Gateway logs | Normal: the HTTP parser accepts GET only; plain probes are dropped pre-handshake with no A2A impact |

## 7. Automated proof

```bash
python -m pytest tests/a2a/ -q   # two isolated instances + real GatewayServer
```

`tests/a2a/test_local_demo.py` runs the entire §5 flow against a
local `GatewayServer` with real sockets, signatures, PostgreSQL, HTTP
routes, and worker loops (deterministic model at the model boundary
only). `tests/a2a/test_hosted_gateway.py` probes a public URL when
`NEXUS_TEST_GATEWAY_URL=wss://…` is set (skipped otherwise).

## 8. What this demo does not prove

- Real provider behavior (LLM quality, latency, cost) — only the
  deterministic stand-in path.
- Two-machine networking (the automated test uses loopback; run §5
  manually for that).
- Production readiness: single-owner auth, in-process event hub,
  in-memory Gateway state, no TLS on the Gateway itself (Render
  terminates it), unbounded worker retries (`max_attempts`
  unenforced), and no MCP tool-iteration cap all stand as documented.
