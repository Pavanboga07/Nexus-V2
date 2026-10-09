# Component 9 — Inbound Message Processor

Receives remote requests, authenticates them, and durably registers
them for recipient approval. It never executes the requested task.

## Flow

`GatewayClient` reader → `incoming` queue → `InboundDispatcher` →
`REMOTE_QUERY` → `InboundProcessor.process()` → request row
(`WAITING_APPROVAL`) + signed message row + audit event (one
transaction) → existing `RequestService.approve()` / `reject()`.

1. The gateway client's single `recv()` loop stays the only reader;
   DELIVERs land on `client.incoming` unchanged.
2. `InboundDispatcher` is the single consumer. `REMOTE_QUERY` goes to
   the processor; every other type is retained in `dispatcher.unhandled`
   for the future response receiver — never silently discarded.
3. The processor runs cheap checks first (type, recipient, payload
   policy), then one transaction: load recipient/sender/peer rows,
   verify the signature against the **stored** sender key, classify
   duplicates, and insert request + message + audit atomically.
4. The result is `REGISTERED` or `DUPLICATE`. Rejections raise typed
   `InboundError`s and leave no rows.

## Verification

- Envelope structure via the existing protocol validators; the signed
  bytes are never modified before verification.
- `verify_message` against the sender's stored `agents.public_key`
  (Ed25519 only): structure, sender/key binding, expiry, signature.
- Sender row must exist and be active; a `connected` peer row
  `(local, sender)` must exist. Unknown, deactivated, key-mismatched,
  or unpeered senders are rejected before any write.
- Recipient must equal the processor's `local_agent_id` exactly, and
  its agent row must exist and be active.
- Payload policy reuses the Request Service limits (`MAX_QUERY_LENGTH`,
  `MAX_CAPABILITIES`, capability syntax); oversized payloads are
  rejected before cryptographic work.
- All timestamps timezone-aware UTC, consistent with Components 1–4.

## Recipient-side representation

No schema change was needed. Each machine owns its database, so B
stores A's `request_id` as its own `remote_requests` primary key with
the original A→B orientation (sender and recipient never reversed).
B's row is born `WAITING_APPROVAL` directly — the outbound
`QUEUED → SENT → …` path describes A's side, not B's. The pending
approval *is* the waiting request: no approval-decision row is written
at registration (that would collide with `approve()`'s unique
decision row). The stored message envelope excludes the signature,
mirroring the outbound write path.

## Duplicates and replay

- **Exact duplicate** (same message, or same request content): returns
  `DUPLICATE`, writes nothing.
- **Conflicting duplicate** (id reused with different content):
  raises `InboundConflictingDuplicate`; stored rows are never
  overwritten, and the rejection itself writes nothing.
- **Concurrency**: PK constraints arbitrate; losers re-read in a fresh
  transaction and classify to `DUPLICATE` or conflict. 8× concurrent
  delivery registers exactly once.
- **Replay**: the `messages` table is the durable replay record
  (survives restarts). An optional in-memory `ReplayRegistry` is
  recorded **only after commit** and never consulted to decide, so a
  crash before commit cannot poison a retry.
- **Infrastructure failure**: connectivity errors become retryable
  `InboundInfrastructureError`; the failed attempt stores nothing, so
  retry succeeds.

## Dispatch and ownership

- One dispatcher per inbound source, enforced: `start()` claims
  exclusive ownership of the bound client's queue
  (`DispatcherAlreadyRunning` for a competitor) and `stop()`,
  cancellation, or consumer failure releases it. A replacement may
  start once ownership is free. Dispatchers on different clients, or
  on raw queue sources with no client, remain independent.
  `stop()` cancels and awaits the consumer task (idempotent) — no
  orphaned tasks.
- `InboundDispatcher.from_client(client, processor)` binds to a
  GatewayClient without modifying its reader, ACK, or queue rules.
- `next_unhandled()` yields retained messages for Component 10.
- Unexpected processor failures are logged with ids only (never query
  content or keys) and reported as `REJECTED`; the loop stays alive.

## Approval boundary

`RequestService.approve()` / `reject()` serve inbound rows unchanged:
recipient-only, one decision, legal transitions only. Approval records
the decision — it does not execute anything in Component 9.

## Limitations / Component 10 boundary

- No response receiver: `REMOTE_RESPONSE` is only retained.
- No REST, WebSocket events, or UI; no LLM, LangGraph, or MCP use.
- No automatic approval, expiry sweeps, or remote response sending.
- Transport delivery (gateway ACK) is not durable acceptance; only a
  committed registration (or explicit `DUPLICATE`) is.
