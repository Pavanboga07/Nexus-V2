# Nexus Outbox Worker & Gateway Client (Component 6)

Dependencies: pinned `websockets`, `cryptography`, SQLAlchemy/asyncpg —
nothing new. No execution, no models, no tools, anywhere.

## Worker responsibility

`OutboxDispatcher` turns committed outbox rows into Gateway handoffs:
claim → load/validate/verify → send → persist the graded outcome. It
transports bytes. It never reasons, never runs tools, never touches
memory, never decides capability policy.

## Outbox claim and lease model

Claims are `SELECT … FOR UPDATE SKIP LOCKED` over due `PENDING` rows:
concurrent workers never own the same row. Each claim stamps a unique
token, a lease expiry, and an attempt count, then commits — network I/O
never holds a transaction open. Every later write is conditional on
the token still owning the row, so a stale worker can never overwrite
a newer outcome: worker paths must use the token-guarded outcome
methods (`mark_sent_if_claimed`, `mark_failed_if_claimed`,
`mark_uncertain_if_claimed`, `reschedule_if_claimed`,
`set_transmission_started`), never the unguarded `mark_sent` /
`mark_failed`, which exist only for non-claim flows.
`transmission_started` is flagged immediately before the first send
attempt so crash recovery can tell "never sent" from "maybe sent".

## Gateway client handshake

`GatewayClient` opens a WebSocket per agent identity and authenticates
with the exact Component 3 handshake (public-identity document + signed
`CONNECT_REQUEST`, Component 2 signing). The server's `WELCOME` must
echo our own id. Connections pool per sender id with a creation lock,
so concurrent dispatches never duplicate a connection.

## ACK correlation

One reader task owns `recv()` and demultiplexes: `ACK` frames resolve
their `message_id` waiter; validated `DELIVER` envelopes land in a
bounded `incoming` queue for a future consumer (a full queue closes
the connection rather than dropping mail). Concurrent sends are safe;
a cancelled wait cleans up its waiter; closing fails all waiters
loudly instead of hanging them.

## Delivery-state semantics

- `SENT` — the Gateway acknowledged handoff (`DELIVERED` or
  `BUFFERED`) under its documented contract. It never means the remote
  task ran or anyone approved anything.
- `RETRYABLE` — a failure provably before transmission (refused
  connection, failed handshake, rate/buffer pressure): back to
  `PENDING` with bounded backoff, identifiers untouched.
- `PERMANENT` — provably undispatchable (malformed/invalid/expired/
  oversized message, bad signature, missing identity, definitive
  Gateway rejection): outbox `FAILED`, request moved to `FAILED`/
  terminal via existing service rules. Never retried. An oversized
  message is rejected by the client before transmission (no byte
  moves), so it lands here rather than as an ambiguous send.
- `UNCERTAIN` — transmission may have occurred but nothing proves the
  outcome (ack timeout, mid-send connection loss, unknown rejection
  codes, signature replay of an already-consumed id, unexpected bugs).
  Preserved verbatim, never auto-retried, never given a new id (that
  would bypass replay protection). Reconciliation belongs to a later
  component — this limitation is explicit.

There is no exactly-once delivery anywhere in this design, by
construction: at-most-once per `message_id`, duplicates suppressed by
Gateway replay + unique constraints + claim tokens.

## Retry/backoff policy

`delay = min(max, base * 2**(attempt-1))`, jittered within
`[delay/2, delay]`, capped (defaults 1 s → 300 s). Attempt counts
persist per claim; error text is bounded static codes. Retry applies
only to pre-transmission failures; anything ambiguous becomes
`UNCERTAIN`.

## Crash recovery

`recover()` sweeps expired leases: never-transmitted claims return to
`PENDING`; possibly-transmitted ones become `UNCERTAIN`. It runs at
every batch start and is safe to run anytime. Leases must outlive one
full send attempt (handshake + transmit + ack timeout);
`WorkerConfig` enforces this at construction
(`lease_seconds > handshake_timeout_s + ack_timeout_s + 30 s margin`,
`ValueError` otherwise); concurrent
workers must respect each other's live leases.

## Multiple workers

`SKIP LOCKED` partitions due rows across live workers; token-guarded
writes serialize outcomes. Two workers never actively own one row
(tested). One caveat: two dispatcher *processes* pooling per-agent
connections independently can trigger the Gateway's
reconnect-replaces behavior — run one dispatcher per identity set.

## Inbound-message queue interface

`client.incoming` (`asyncio.Queue`, bounded, default 100) plus
`client.receive_inbound(timeout)` for the future consumer. The worker
does not consume it.

## Shutdown behavior

`stop()` is cooperative: the in-flight batch completes (bounded),
no new batch starts, then pooled connections close. External
cancellation propagates after the same cleanup. `run_once()` on an
empty queue returns `[]`; repeated calls are safe.

## Limitations of in-memory Gateway buffering

A `BUFFERED` ack means bytes sit in the Gateway's process memory and
vanish on Gateway restart — the outbox still records `SENT`
(handoff happened). Crash windows (record-before-transmit,
transmit-before-ack) resolve to at-most-once + `UNCERTAIN`
preservation, never to silent loss or blind redelivery.

## Claim → Connect → Send → ACK → Persist Outcome

```text
run_once()
  │ recover stale claims
  │ claim PENDING rows (SKIP LOCKED, token+lease, COMMIT)
  v
per row:
  load envelope → validate → verify signature (local sender key)
  → flag transmission_started (COMMIT)
  → pooled connection ensure_connected()
  → send signed bytes → await correlated ACK
  v
  DELIVERED/BUFFERED → outbox SENT + request QUEUED→SENT (atomic)
  retryable pre-send → PENDING + backoff
  permanent         → outbox FAILED (+ request FAILED/terminal)
  ambiguous         → UNCERTAIN (preserved)
```

Ambiguous-outcome path: any send that may have moved bytes without a
decisive ack — timeout, connection loss mid-flight, unknown rejection
codes, consumed-id replays, unexpected bugs — lands in `UNCERTAIN`
with the original ids and signed bytes intact, awaiting a reconciler
that does not exist yet.
