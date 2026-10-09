# Nexus Identity & Cryptographic Security (Component 2)

Dependency: the established `cryptography` package (Ed25519 only; no
PyNaCl, no second crypto library). Standard library otherwise.

## Identity model

An agent owns exactly one Ed25519 key pair plus the id derived from it:

```text
AgentIdentity (secret, stays local)
├── agent_id:    "agent_<sha256-hex>"
└── private_key: Ed25519PrivateKey   (repr-hidden, never serialized outward)

PublicIdentity (distributable to peers / Gateway)
├── agent_id:    same deterministic id
├── public_key:  raw 32 bytes
└── algorithm:   "Ed25519"
```

`AgentIdentity.public_identity()` projects the safe subset. The private
key never appears in the public document, its dict form, logs, or any
protocol message (no logging exists in this component at all).

## Agent ID derivation

```text
raw = Ed25519 public key bytes (32)
agent_id = "agent_" + lowercase_hex(SHA-256(raw))
```

Example: `agent_b81679e6…` (64 hex chars). Derived from the public key
only: no usernames, no database ids, no mutable metadata — reloading the
same private key re-derives the same id, and distinct keys collide only
with SHA-256-preimage improbability. `verify_agent_identity(agent_id,
public_key)` re-derives and compares (total predicate: returns `False`
instead of raising on bad input).

## Public/private key model and serialization

Raw 32-byte form for both keys (the `cryptography` library's standard
raw serialization; no custom file encryption in this component —
key-at-rest encryption is a later deployment concern):

- `private_key_to_bytes` / `private_key_from_bytes`
- `public_key_to_bytes` / `public_key_from_bytes`

Import validation rejects empty input, wrong length (must be 32 bytes),
malformed bytes, non-bytes, and — for typed key objects — the wrong key
scheme (`isinstance` gates, e.g. an RSA key object is refused). Raw
bytes carry no scheme tag, so a 32-byte foreign key is indistinguishable
on the wire; scheme safety comes from typed objects plus the
agent-id consistency check. `identity_from_private_bytes` reloads a full
identity whose public key and id are re-derived, hence consistent by
construction.

## Signing flow

```text
typed NexusMessage (unsigned; signature may be "")
      ↓  1. structural validation (signature presence exempted —
      ↓     signing runs pre-signing; all other Component 1 rules apply)
      ↓  2. sender == agent_id derived from the signing private key
      ↓     (signing never attests another agent's identity)
      ↓  3. signing_bytes = canonicalize_message(message)
      ↓     (Component 1; signature field excluded by construction)
      ↓  4. raw = Ed25519.sign(signing_bytes)   [deterministic]
      ↓  5. signature = base64url(raw)
      ↓
new NexusMessage (input never mutated; only signature differs)
```

`sign_message(message, private_key) -> NexusMessage`. Re-signing an
unchanged message yields the identical signature (Ed25519
determinism). Expiry is deliberately not enforced at signing;
acceptance is the verifier's job.

## Verification flow

`verify_message(message, public_key, *, now=None, replay_registry=None)
-> VerificationResult(agent_id, message_id, request_id)`.

Check order and rationale:

```text
1. protocol structure  → reject garbage before any trust reasoning
2. sender/key binding  → valid signature ≠ valid claimed identity;
                         the claimed sender must re-derive from the key
3. expiry              → stale-but-authentic messages are refused
4. replay membership   → duplicates refused; the id is RECORDED only
                         after signature success, so forged messages
                         cannot pollute the registry (one shared clock:
                         the caller's `now` drives both expiry and
                         membership checks)
5. signature           → Ed25519 over Component 1 canonical bytes
```

Failures raise dedicated errors (`InvalidSignatureError`,
`MalformedSignatureError`, `ExpiredMessageError`,
`ReplayDetectedError`, `SenderMismatchError`, `InvalidPublicKeyError`);
structural problems propagate Component 1's `ProtocolValidationError`.
The library's `InvalidSignature` is wrapped — raw crypto exceptions
never escape. Mutating any signed field (payload, sender, recipient,
ids, timestamps, version, type, signature) breaks verification.

Receivers must follow receive → verify → validate/use and never act on
unverified content.

## Signature encoding

```text
raw 64-byte Ed25519 signature
  → base64url, "=" padding stripped (compact, unambiguous alphabet)
  → protocol `signature` string (opaque non-empty, schema unchanged)
```

Decoding restores padding and enforces 64 bytes; undecodable or
wrong-length values raise `MalformedSignatureError`.

## Replay protection

`ReplayRegistry`: thread-safe in-memory map `message_id → expires_at`
with `record`, `has_seen` (lazily drops expired entries), atomic
`check_and_record` (exactly one winner under concurrency),
`purge_expired`, all keyed by `message_id` — never `request_id`, since
siblings (`msg_001`, `msg_002`, … under one `req_123`) are legitimate.
Persistence and gateway buffering arrive later; this is the primitive
those layers will build on.

## Expiry

Component 1 timestamps are reused verbatim — no second format, no second
system, semantics unchanged. Expired wire data still deserializes, but
security verification refuses it (`ExpiredMessageError`). Comparisons
are timezone-aware UTC; naive `now` is rejected via Component 1 rules.
The `created_at == expires_at` boundary follows Component 1 (rejected
at parse).

## Sender/key consistency

`verify_agent_identity` plus the step-2 binding prove the supplied key
owns the claimed sender id. Verifying A's message with B's key fails
with `SenderMismatchError` before cryptography is even attempted.

## Security boundaries

Component 2 answers: "Is this message cryptographically authentic and
within its validity window?" It does NOT answer: "May this sender use
`calendar.read`?" — capability authorization, human approval, user
authentication, peer discovery, persistence, routing, transport, agents,
and LLM/tooling all belong to later components and are absent here.

## Future integration points

- Gateway/peer lookup resolves `agent_id → PublicIdentity`
  (`public_identity_from_dict` validates distributed documents).
- Request persistence will back replay state across restarts.
- Authorization will consume `VerificationResult.agent_id` as the
  authenticated sender — never the unverified `sender` field.

Run tests with `python -m pytest tests/` from the repo root.
