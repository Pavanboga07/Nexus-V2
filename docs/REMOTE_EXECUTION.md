# Component 10 — Remote Execution Engine

Runs explicitly approved inbound requests through the recipient's own
LangGraph Agent Runtime and permitted MCP tools, then durably queues a
signed `REMOTE_RESPONSE` for the existing outbox worker. It never
receives responses (Component 11) and never exposes an API surface.

## Eligibility

`RemoteExecutor.execute(request_id)` re-verifies everything inside the
claim transaction — never on caller assertion, signatures, delivery
status, or an `approved` flag:

- Row exists, status is `APPROVED`, recipient is the executor's local
  agent, sender is a different (peer) agent.
- Sender/recipient rows exist and are active.
- An `approved` decision row by the local recipient exists (`APPROVED`
  status alone is insufficient — an outbound row approved elsewhere and
  a hand-built `APPROVED` row are both rejected).
- Peer `(local, sender)` is still `connected`.
- `expires_at` is in the future; an expired row moves to `EXPIRED` in
  its own transaction and is never executed.
- The local agent's signing identity is available
  (`private_key_for(local_agent_id)` succeeds). This is checked inside
  the claim transaction *before* `APPROVED → PROCESSING` commits and
  before the Agent Runtime runs: a missing key raises `ExecutionError`,
  the row stays `APPROVED` (recoverable once the identity is
  restored), and no model or MCP tool ever runs.

Anything else raises a typed error with zero model or tool calls.

## Claim and states

The claim is `APPROVED → PROCESSING` under a PostgreSQL row lock in
one transaction — durable across processes, no memory-only guards. The
same transaction writes an execution lease (owner, unguessable fencing
token, expiry, heartbeat, attempt number) plus an `EXECUTION_STARTED`
audit fact. Concurrent callers serialize on the lock: exactly one
executes, the rest report `IN_PROGRESS` without touching the runtime.
`COMPLETED` and `FAILED` replay their stored outcome (`executed=False`).

While the runtime works, a heartbeat extends the lease only while its
fencing token still owns the `PROCESSING` row. A crash or partition
stops the heartbeat; once the lease expires, `execute` (or the recovery
endpoint) records `PROCESSING → RECOVERABLE` with
`EXECUTION_LEASE_EXPIRED` and clears the lease. `PROCESSING` with a live
lease is sticky: later calls report `IN_PROGRESS` and never re-enter the
runtime. There is **no automatic retry** — an operator must choose:

- `POST /requests/{id}/recover {"action":"retry"}` → `RECOVERABLE →
  APPROVED`; the normal executor re-verifies approval, peer trust,
  expiry, and signing identity before a new attempt. Side effects from
  the abandoned attempt may already have happened.
- `POST /requests/{id}/recover {"action":"resolve"}` → `RECOVERABLE →
  FAILED` with `OPERATOR_RESOLVED`; nothing runs.

Legacy `PROCESSING` rows with a `NULL` lease predate this contract and
are deliberately excluded from automatic recovery. Finalization checks
the fencing token as well as `PROCESSING`, so a stale worker whose lease
was recovered cannot overwrite the newer attempt's state.

## Running the agent

The approved query runs via the real `AgentRuntime.run(query,
history=None)` — the compiled Component 7 graph with the recipient's
model and configured MCP allowlist tools. The remote query is user
input only; the trusted local system instruction is unchanged;
history is always empty so executions share no conversation. Remote
`requested_capabilities` never widen tool access: the runtime's
pre-built tool set governs, and unknown tool calls are rejected by the
existing router before execution. Input/output bounds, argument
bounds, result bounds, and the runtime timeout all apply unchanged.

## Responses and outbox

Finalization commits response message + outbox row + terminal state
atomically, guarded by re-checking both `PROCESSING` and the execution
lease's fencing token (a recovery or operator `fail()` blocks the write
instead of being overwritten):

- Success: `REMOTE_RESPONSE` (`status="ok"`, agent's final text),
  signed by the local agent, `request_id` preserved, fresh
  `message_id`, sender/recipient set to executor/requester,
  expiry inherited from the request. Success additionally requires
  more than `RESPONSE_EXPIRY_MARGIN_S` (10 s) of remaining lifetime at
  finalization: a response expiring at or within the margin would pass
  the executor's check yet fail the outbox worker's existing expiry
  validation on the next dispatch, leaving an undeliverable
  `COMPLETED`. Such near-expiry successes take the expiry failure
  path below instead — no success is claimed and no second run occurs.
- Agent/model failure: `REMOTE_RESPONSE` (`status="error"`,
  `error_code="EXECUTION_FAILED"`, static safe message — never
  exception text, tool output, or keys) plus `FAILED` state.
- Request expiring mid-flight: `FAILED` (`REQUEST_EXPIRED`) with no
  response, since no valid envelope can be built.

The outbox row (`PENDING`) is picked up by the existing Component 6
worker with no rewriting or re-signing; a confirmed handoff on a
terminal request records outbox `SENT` without a request transition
(see `_persist_confirmed`). Gateway `SENT`/`DELIVERED`/`BUFFERED` is
transport acceptance, never proof the requester processed the answer.

## Crash cases

1. Before the claim — nothing stored; retry safe.
2. After claim, before the run — `PROCESSING` with a lease; later calls
   report `IN_PROGRESS` until the lease expires, then `RECOVERABLE`.
3. During model/tools — the heartbeat keeps the lease live; uncertain
   side effects are never automatically duplicated.
4. After the run, before recording — the lease expires; the request
   becomes `RECOVERABLE` and the stale result is dropped rather than
   risk a double response. A stale finalize is blocked by the fencing
   token even if the request was explicitly retried.
5. After recording, before delivery — the worker delivers; repeat
   calls replay the stored response id.

## Future interface

Components 12+ trigger execution with
`RemoteExecutor(sessions, local_agent_id, identities,
runtime).execute(request_id)` — no MCP, crypto, or outbox knowledge
required. MCP lifecycle stays with the application wiring (the
executor never opens or closes the manager).

## Deferred to Component 11

Receiving `REMOTE_RESPONSE` on the originating machine, response
correlation there, and any requester-side retry policy.
