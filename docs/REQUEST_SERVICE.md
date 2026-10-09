# Nexus Request Service (Component 5)

No new runtime dependencies: Component 1 (protocol), Component 2
(identity), Component 4 (persistence/SQLAlchemy). No network, no agents.

## Responsibility

The Request Service owns the durable lifecycle of a logical remote
operation: creation, state, approval records, and response correlation.
It prepares signed, persisted, dispatch-ready requests. It never sends
bytes, never reasons, never executes tools, and never decides
capability policy — "is this the recipient?" yes;
"is `calendar.read` allowed?" never.

## Request lifecycle

```text
CREATED → QUEUED → SENT → DELIVERED → WAITING_APPROVAL → APPROVED
    → PROCESSING → COMPLETED
```

Terminal: `REJECTED`, `EXPIRED`, `FAILED`, `CANCELLED`, `COMPLETED`.
`CREATED` is the pre-commit concept; rows are born `QUEUED` once
request + message + outbox commit together. Every move goes through
one transition map (`states.require_transition`), so illegal moves —
including any resurrection of a terminal state — raise
`InvalidStateTransitionError` at exactly one place.

Gateway delivery states (`ACCEPTED/DELIVERED/BUFFERED/FAILED`) are
transport facts and are never equated with lifecycle states: a
`BUFFERED` Gateway ack means the bytes wait at the Gateway, not that
any agent received anything. `mark_delivered` records only
application-level receipt as reported by later components.

## Request creation

```text
validate input (query, capabilities, expiry, ids)
  ↓
load sender (must exist + active), recipient (must exist),
peer row (must exist + status connected)
  ↓
build REMOTE_QUERY envelope (Component 1, structurally validated)
  ↓
sign with the sender's key via Component 2
(sender binding enforced by the signer itself)
  ↓
BEGIN
  ├── insert remote_requests (QUEUED)
  ├── insert messages (envelope minus signature + signature)
  └── insert outbox_messages (PENDING)
COMMIT
  ↓
return RequestRecord (QUEUED)
```

The transaction is all-or-nothing: any failure (duplicate ids,
constraint violation, signing failure) leaves zero durable trace.
`request_id`/`message_id` default to fresh `req_`/`msg_` ids; callers
may supply explicit identifiers instead, but those are NOT idempotency
keys. Reusing a stored `request_id` — with identical or different
content — raises a conflict without creating or overwriting anything,
and likewise for `message_id`: `request_id` names one logical
operation, `message_id` names one physical message, and every new
physical message needs its own unique id. Client-level idempotency
(return-the-prior-result retry semantics) is deferred to a future API
layer. No second identity exists.
Private keys come from the injected `IdentityProvider` port — never
from PostgreSQL, never from globals.

## Message creation

`message_builder.build_remote_query_message` constructs the
REMOTE_QUERY envelope (sender, recipient, request_id, fresh
message_id, query, capabilities, timestamps) and structurally
validates it pre-signing. The service signs with Component 2 and
persists the exact wire mapping; receivers re-verify the identical
bytes after reload.

## Signing

Keys arrive via `IdentityProvider.private_key_for(agent_id)`
(`KeyError` → service error). `sign_message` binds sender to key, so
a mismatched provider key fails closed with nothing persisted.

## Transactional outbox

`outbox_messages` rows (`PENDING` → `PROCESSING` → `SENT`/`FAILED`,
attempt counts, bounded error text, availability window) are created
in the creation transaction. The service never dispatches; Component
6 will claim rows with `SELECT … FOR UPDATE SKIP LOCKED`
(`claim_pending`: concurrent workers never share a row) and mark
outcomes. The outbox references the stored physical message; no
secrets are duplicated into it.

## Approval

`approve`/`reject` require `WAITING_APPROVAL`, require the caller to
be the recipient agent (`ApprovalError` otherwise), write the
`approvals` row (unique per request — second decisions fail) and move
state in one transaction. No execution, no policy, no Gateway calls.

## Response correlation

```text
receive envelope
  ↓
parse (must be REMOTE_RESPONSE) → locate request by request_id
  ↓
local agent must be the requester; envelope parties must mirror
the original direction (responder = recipient, audience = sender)
  ↓
request must be APPROVED or PROCESSING (lag-tolerant, never
pre-approval, never terminal: no resurrection, ever)
  ↓
verify Ed25519 against the recipient's stored public key
  ↓
BEGIN → persist response message → attach response_message_id →
COMPLETED (answer ok) or FAILED (answer error, code/message kept)
COMMIT
```

Correlation key is `request_id`; identity key is `message_id` —
duplicate redeliveries meet the terminal guard (or the unique
constraint) and resolve deterministically to `ResponseCorrelationError`.
Late responses to terminal requests are rejected, not resurrected.

## Failure handling

Explicit errors for unknown/inactive agents, missing or
non-connected peers, invalid input, unknown requests, illegal moves,
approval misuse, correlation failures, expiry (`expires_at`-checked,
never confused with transport timeouts), rejection, local
cancellation, and conflicts. Unexpected persistence failures keep
their chain; private keys and query content never appear in errors.

## Concurrency model

Row locks (`get_for_update`) serialize transitions per request, so
approve-vs-reject and double-transition races yield exactly one
winner; losers get explicit state/conflict errors. PostgreSQL unique
constraints backstop identity races. No distributed claims beyond
real row locking; no serializability promises beyond it.

## What Component 5 does NOT do

No Gateway dispatch or WebSocket I/O, no background worker, no
LangGraph/LLM/MCP/memory work, no REST/frontend, no capability
authorization, no approval policy, no encryption, no embeddings.
