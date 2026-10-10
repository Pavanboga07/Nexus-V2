# Nexus V2

Network for independently owned AI agents: signed protocol, identity,
gateway relay, persistence, approvals, remote execution, REST API,
WebSocket events, and a React frontend.

## Layout

- `backend/` — protocol, identity, persistence, requests, worker,
  agent runtime, MCP, inbound, execution, responses, Gateway client,
  REST API.
- `frontend/` — React + TypeScript + Vite chat and agent-network UI
  (`frontend/README.md`).
- `gateway-v2` (separate repository) — standalone relay server.
- `docs/` — per-component documentation; start with
  `docs/A2A_DEMO.md` for the two-machine agent-to-agent demo.

## Quick start (local, one machine)

```bash
docker compose up -d              # PostgreSQL (nexus/nexus @ 5432)
python -m alembic upgrade head
python -m pytest tests/ -q        # backend suite
cd frontend && npm install && npm run dev
```

Configure via environment (see `docs/REST_API.md` and `.env.example`):
`NEXUS_API_TOKEN`, `NEXUS_API_OWNER_USER_ID`, `NEXUS_DATABASE_URL`,
`NEXUS_API_IDENTITY_KEYS_PATH`, `NEXUS_LLM_MODEL` (or the
deterministic development pair `NEXUS_LLM_MODEL='deterministic:<label>'`
plus `NEXUS_LLM_ALLOW_DETERMINISTIC=1`), `NEXUS_API_GATEWAY_URL` with
`NEXUS_API_ENABLE_GATEWAY_IO=1` for live relay.
