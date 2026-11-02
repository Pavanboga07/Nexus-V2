# Dependency Policy — Application and Relay

The Nexus application and the standalone gateway run in **separate
processes** and share only the wire protocol plus the
`backend.protocol` / `backend.identity` modules. They intentionally
pin different `websockets` versions because their constraints differ.

## Current pins

| Component | Pin | Reason |
|---|---|---|
| Nexus-V2 application | `websockets==16.1.1` | `langgraph-sdk 0.4.5` (via `langgraph==1.2.12`) declares `websockets<17,>=14`. Upgrading the app runtime would require a coordinated LangGraph/SDK upgrade, not a one-line bump. |
| gateway-v2 relay | `websockets==17.2` | `websockets>=17` parses non-GET request lines, so plain `HEAD /healthz` probes reach the application's HTTP interceptor instead of failing inside the handshake parser. |
| Shared protocol/identity | `nexus-v2 @ git+…@61b05ac` | The relay imports only `backend.protocol` and `backend.identity`. The SHA pins the exact protocol version it was audited against. |

## Why this is safe

- The two processes communicate over WebSocket frames containing Nexus
  protocol envelopes. The `websockets` library version is a local
  transport implementation detail, not part of the wire contract.
- The relay has no dependency on the application's LangGraph, MCP,
  persistence, API, or model stack. It must not install those packages.
- Protocol compatibility is checked explicitly: `/healthz` exposes
  `protocol_version`, and `python -m gateway.smoke` completes a real
  signed handshake against the deployed relay.

## Install contract

The gateway's `pyproject.toml` declares the pinned `nexus-v2`
distribution so package metadata is truthful. Deployments and CI still
install it with `--no-deps` and then install the relay's own runtime
pins explicitly:

```bash
pip install --no-deps "nexus-v2 @ git+https://github.com/Pavanboga07/Nexus-V2.git@61b05acd0895656769e9ed7469839ebe48add4bc"
pip install --no-deps -e .
pip install cryptography==46.0.5 websockets==17.2
```

`--no-deps` is not a workaround for an undeclared dependency; it is the
boundary that keeps the application's heavy stack out of the relay
image. A normal `pip install .` remains possible for consumers that
want the full application dependency set.

## Bump procedure

1. Change the application pin only after `langgraph-sdk` admits the new
   `websockets` range; run the backend suite.
2. Change the relay pin only after its full suite passes under the new
   version, including `tests/test_health.py::test_head_probes_answered_cleanly`.
3. Advance the shared `nexus-v2` SHA in `gateway-v2/pyproject.toml`,
   `render.yaml`, `.github/workflows/ci.yml`, and the deploy-smoke
   workflow together.
4. Run `python -m gateway.smoke --url …` against staging before
   production.
5. Update this table and `docs/GATEWAY.md` in the same change.

CI enforces each side independently: the application job installs its
declared pins and runs the backend suite against PostgreSQL; the relay
job installs 17.2 in a clean environment and runs the full gateway
suite.
