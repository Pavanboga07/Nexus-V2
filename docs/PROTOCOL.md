# Nexus Protocol (Component 1)

## Purpose

The Nexus Protocol defines the exact structure of messages exchanged between
independently owned Nexus agents through the Nexus Gateway. It is a contract
package only: it defines message shapes, validation, serialization, and the
deterministic canonical form used for future signing. It executes no behavior
(no routing, auth decisions, LLM calls, tool calls, or storage).

Scope boundaries (enforced, not just documented):

- No FastAPI / HTTP / WebSocket imports (transport-independent).
- No database imports (id generation uses `uuid4`, no persistence).
- No LangGraph / LangChain / MCP imports.
- No cryptographic imports (signing arrives in Component 2).
- Usable by both the Nexus Agent and the Nexus Gateway.

Package layout (`backend/protocol/`):

- `types.py` — version constant, `MessageType` enum, id/capability syntax,
  `generate_message_id` / `generate_request_id`.
- `models.py` — typed envelope (`NexusMessage`) and per-type payload
  dataclasses. The envelope is the primary representation; raw dicts are a
  wire format only.
- `validation.py` — structural validation; raises
  `ProtocolValidationError`. Never makes authorization decisions.
- `serialization.py` — typed message `<->` JSON; raises
  `ProtocolSerializationError`. Strict: no silent coercion.
- `canonicalization.py` — `canonicalize_message(message) -> bytes`.

Tests live in `tests/protocol/` (run: `python -m pytest tests/` from repo
root; stdlib + pytest only, zero third-party dependencies).

## Envelope

Every message contains exactly these fields, in this protocol order:

| Field | Type | Meaning |
|---|---|---|
| `protocol_version` | string | Must be `"1.0"`; anything else is rejected |
| `message_id` | string | Identifies one physical protocol message |
| `request_id` | string | Identifies one logical remote operation |
| `sender` | string | Originating agent id |
| `recipient` | string | Destination agent id |
| `type` | string | One of the seven message types |
| `created_at` | string (ISO-8601) | Timezone-aware creation time |
| `expires_at` | string (ISO-8601) | Must be after `created_at` |
| `payload` | object | Type-specific content (see below) |
| `signature` | string | Opaque, non-empty; reserved for Component 2 |

Wire objects with missing or unknown envelope fields are rejected.

## Identifier semantics

- `message_id` = one physical message (`msg_001`, `msg_002`, `msg_003`, ...).
- `request_id` = one logical remote operation (`req_123` shared by all of
  the above).
- Correlation: a response carries the *same* `request_id` as the query it
  answers, under a *new* `message_id`, so Agent A can match responses to
  outstanding requests.
- Syntax (deterministic, database-free): `^[A-Za-z0-9][A-Za-z0-9_\-.:]{0,127}$`
  for `message_id`, `request_id`, `sender`, `recipient`, and agent references.
- `generate_message_id()` / `generate_request_id()` produce unique
  `msg_<hex>` / `req_<hex>` ids via `uuid4`.

## Message types and payloads

### CONNECT_REQUEST

Connection-request data structure (no connection logic).

- `agent_id` (string, agent-id syntax): requesting agent reference.
- `display_name` (optional string): human-readable name.
- `context` (optional string): requested connection context.

### CONNECT_RESPONSE

Connection decision (no state management).

- `accepted` (boolean, strict: `1`/`0` are rejected).
- `responder_agent_id` (string): who decided, for correlation.
- `reason` (optional string).

### REMOTE_QUERY

- `query` (non-empty string): natural-language query. The protocol only
  transports it; it never interprets it.
- `requested_capabilities` (list of strings, e.g. `["memory.read"]`).
  Must be a list; each entry must match
  `^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$`.

Example payload:

```json
{"query": "What projects are you currently working on?",
 "requested_capabilities": ["memory.read"]}
```

### APPROVAL_REQUEST

Structured information so User B can decide (no approval logic here).

- `request_ref` (string, request-id syntax): the logical operation.
- `query` (non-empty string): original query/context.
- `requested_capabilities` (list, same rules as `REMOTE_QUERY`).
- `context` (optional string).

### APPROVAL_RESPONSE

- `approved` (strict boolean).
- `reason` (optional string).

### REMOTE_RESPONSE

- `status`: `"ok"` or `"error"` (nothing else accepted).
- `answer`: string content; must be non-empty when `status` is `"ok"`.
- Optional: `structured_data` (object), `citations` (list of strings),
  `error_code` / `error_message` (strings).
- The envelope's `request_id` ties the response to the original query.

### ERROR

- `error_code`: `^[A-Z0-9_]{1,64}$` (e.g. `NOT_FOUND`).
- `message`: non-empty string.
- `retryable`: strict boolean.
- Payloads carry plain strings only: never stack traces, filesystem paths,
  secrets, or internal database details.

## Versioning

- Initial version: `1.0` (`PROTOCOL_VERSION`; `SUPPORTED_PROTOCOL_VERSIONS = ("1.0",)`).
- Every message carries `protocol_version` explicitly.
- Unsupported versions are rejected at validation and deserialization.
- No future-version negotiation is implemented (out of scope).

## Timestamp rules

- `created_at` / `expires_at` are timezone-aware datetimes, UTC
  representation, ISO-8601 compatible on the wire
  (e.g. `2026-01-01T12:00:00+00:00`; trailing `Z` is accepted on input).
- Naive (timezone-less) or malformed timestamps are rejected.
- `expires_at` must be strictly after `created_at` (checked at parse time,
  so ordering violations never construct a message).
- Expiry against the clock is a *validation-time* check
  (`validate_message(..., check_expiry=True)` / `is_expired(message, now)`),
  so already-stored messages still parse. No clock synchronization is
  implemented.

## Canonicalization

Critical for Component 2. `canonicalize_message(message) -> bytes` maps a
message to deterministic bytes:

```
message  ->  canonical dict (signature dropped, timestamps normalized)
         ->  json.dumps(sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False)
         ->  UTF-8 bytes  ->  future Ed25519 signing (Component 2)
```

Guarantees:

- Deterministic field ordering (keys sorted recursively, including payload).
- No whitespace dependence (`separators=(",", ":")`).
- Consistent UTF-8 encoding.
- `signature` is excluded from the signed content (including it would be
  circular: it is the signature *over* these bytes).
- Timestamps normalized to UTC ISO-8601, so equivalent instants written with
  different offsets/`Z` spellings canonicalize equally.
- The same logical message always yields identical bytes; changing any
  signed field (including `request_id`) changes the bytes.

No custom canonicalization scheme was invented: this is plain deterministic
JSON (`sort_keys`, compact separators, UTF-8).

### Unicode

Canonicalization operates on JSON/code-point values with UTF-8 encoding.
No Unicode normalization (such as NFC) is applied: strings that differ
only by normalization form (e.g. NFC vs. NFD) canonicalize to different
bytes. This is stable in practice because JSON transports preserve exact
code points end to end. Verifiers must compare canonical bytes as-is and
must not renormalize text before verifying.

## Future signature integration

- The envelope already contains `signature` (opaque non-empty string), so
  Component 2 can attach Ed25519 signatures without changing the envelope.
- This component never generates, verifies, or parses signatures and
  imports no cryptographic libraries.
- Component 2 contract: sign `canonicalize_message(message)` bytes with the
  sender's Ed25519 key; store the signature encoding in `signature`;
  verifiers recompute `canonicalize_message` (which drops `signature`) and
  verify against it.

### Sign-then-send atomicity

Messages are mutable Python objects while canonical bytes are a snapshot,
so future signing must follow this order:

```text
finalize message
      ↓
canonicalize
      ↓
sign canonical bytes
      ↓
send unchanged message
```

A message must not be modified after signing: any post-signing mutation
invalidates the signature, which is exactly what verification must catch.
Conversely, receivers must process messages in this order:

```text
receive
  ↓
verify
  ↓
validate/use
```

Never interpret or act on unverified content first.

## Serialization rules

- `message_to_json` is deterministic (`sort_keys=True`, compact separators).
- `message_from_json` rejects malformed JSON, non-object JSON, and non-UTF-8
  bytes.
- `message_from_dict` rejects missing/unknown envelope fields, unknown
  payload fields, wrong types, bad timestamps, bad ids, unsupported versions,
  and unknown message types.
- `message_to_dict` validates before emitting: a non-`MessageType` type or
  a payload that does not match the message type is rejected with
  `ProtocolSerializationError`, so invalid typed state never becomes wire
  data. (Signature presence is not required at emit time because
  canonicalization runs pre-signing.)
- `structured_data` (REMOTE_RESPONSE) must be recursively JSON-compatible
  (`None`/bool/int/finite float/str without lone surrogates/lists/dicts
  with string keys); anything else fails validation, so a validated
  message is guaranteed serializable and canonicalizable.
- Payload dicts omit `None` optionals; parsing treats absent optionals as
  `None` (round-trip stable).
- Never coerces: `"true"` is not a boolean, `1` is not `True`,
  `"memory.read"` is not `["memory.read"]` — all are errors.

## Validation rules

`validate_message` checks: version, type, ids, sender/recipient, timestamp
shape and ordering, payload-type match, payload structure, signature presence,
and (optionally) expiry. Errors are returned as a list of clear strings, or
raised as `ProtocolValidationError`.

Structural vs. authorization examples:

- Protocol validation: `requested_capabilities must be a list`.
- NOT protocol validation: `agent A is allowed to use calendar.read`
  (authorization belongs to another component; the LLM is never the
  security authority).

## Example messages

REMOTE_QUERY:

```json
{"protocol_version": "1.0", "message_id": "msg_001",
 "request_id": "req_123", "sender": "agent:alice",
 "recipient": "agent:bob", "type": "REMOTE_QUERY",
 "created_at": "2026-01-01T12:00:00+00:00",
 "expires_at": "2026-01-01T13:00:00+00:00",
 "payload": {"query": "What projects are you currently working on?",
             "requested_capabilities": ["memory.read"]},
 "signature": "<ed25519-signature-added-by-component-2>"}
```

REMOTE_RESPONSE (same `request_id`, new `message_id`):

```json
{"protocol_version": "1.0", "message_id": "msg_003",
 "request_id": "req_123", "sender": "agent:bob",
 "recipient": "agent:alice", "type": "REMOTE_RESPONSE",
 "created_at": "2026-01-01T12:05:00+00:00",
 "expires_at": "2026-01-01T13:05:00+00:00",
 "payload": {"status": "ok", "answer": "Project Nexus.",
             "structured_data": {"projects": ["nexus"]},
             "citations": ["memory:001"]},
 "signature": "<ed25519-signature-added-by-component-2>"}
```

ERROR:

```json
{"protocol_version": "1.0", "message_id": "msg_009",
 "request_id": "req_123", "sender": "agent:bob",
 "recipient": "agent:alice", "type": "ERROR",
 "created_at": "2026-01-01T12:06:00+00:00",
 "expires_at": "2026-01-01T13:06:00+00:00",
 "payload": {"error_code": "NOT_FOUND",
             "message": "no such agent", "retryable": false},
 "signature": "<ed25519-signature-added-by-component-2>"}
```
