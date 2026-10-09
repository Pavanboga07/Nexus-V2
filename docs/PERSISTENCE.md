# Nexus Persistence (Component 4)

Dependencies: `SQLAlchemy==2.0.35`, `asyncpg==0.29.0`, `alembic==1.13.0`
(pinned in `pyproject.toml`). No other persistence libraries.

## Database architecture

PostgreSQL 16 is the source of truth for durable Nexus application
state. Access flows one way: future services (Request Service, API,
Worker, Agent Runtime) → repositories → `AsyncSession` → PostgreSQL.
Repositories take an explicit session per call — there is no global
or hidden session state. The Gateway stays in-memory by design; its
buffering is untouched by this component.

## Tables

- `users` — local human owners (`id` UUID, timestamps). No
  credentials, passwords, OAuth, or sessions: authentication is later.
- `agents` — known agents keyed by the Component 2 `agent_id`
  (`agent_<sha256-hex>`, primary key, never re-derived here).
  `owner_user_id` references `users` and is NULL for known peer agents
  (both request endpoints must satisfy their foreign keys, and peers
  are not locally owned). `public_key` is base64url text,
  `key_algorithm` is `"Ed25519"`, `active` defaults true.
- `agent_peers` — directed logical peering (`local_agent_id`,
  `peer_agent_id`), unique per pair, database-enforced no-self-peer
  rule, status CHECK (`pending`/`connected`/`revoked`). This is NOT
  transport presence; statuses are stored, never interpreted.
- `remote_requests` — one row per logical remote operation, keyed by
  protocol `request_id`. Both endpoints reference `agents`.
  `requested_capabilities` is JSONB, `query` is text, timestamps are
  `timestamptz`. Status is stored text; Component 5 owns transitions.
  `response_message_id` carries no foreign key (it would cycle
  requests → messages → requests); Component 5 resolves it.
- `messages` — one row per physical protocol message, keyed by
  protocol `message_id`. `request_id` references `remote_requests`
  (Component 1 requires it on every envelope). Sender/recipient are
  indexed text, not foreign keys: counterparties are often peers
  without local agent rows. `envelope` is the exact wire mapping minus
  `signature` (JSONB); `signature` sits beside it for inspection.
- `approvals` — one decision row per request (`request_id` unique).
  Decision CHECK (`approved`/`rejected`). Recording only: nothing here
  changes request state.
- `conversations` / `conversation_messages` — durable chat history
  (role CHECK `user`/`agent`/`system`), optional `request_id`
  correlation, optional JSONB metadata. No embeddings, no search.
- `audit_events` — append-only facts (`event_type`, optional actor /
  request / message correlation, timestamps, bounded JSONB metadata).
- `outbox_messages` — transactional dispatch queue (Component 5):
  one row per physical message (`message_id` unique, references
  `messages`), lifecycle `PENDING` → `PROCESSING` → `SENT`/`FAILED`,
  attempt counts, bounded error text, availability window. Queue state
  only; dispatch behavior belongs to the future worker.
- `remote_requests.conversation_id` — optional correlation to a
  conversation thread (no foreign key, same rationale as
  `response_message_id`: threads evolve independently).

## Relationships

```text
users 1──* agents (owner, nullable for known peers)
agents 1──* agent_peers (local side; peer side references agents too)
agents 1──* remote_requests (as sender; as recipient)
remote_requests 1──* messages (by request_id)
remote_requests 1──0..1 approvals (unique request_id)
agents 1──* conversations 1──* conversation_messages
audit_events (correlates loosely by request_id / message_id, no FKs)
```

No circular foreign keys. Indexes cover owner, peer, sender,
recipient, status, type, ids, and timestamps per lookup pattern —
deliberately, not exhaustively.

## IDs

Protocol identifiers (`agent_id`, `request_id`, `message_id`) are
stored verbatim as `VARCHAR(128)` — the same 128-character bound as
Component 1 validation, so oversized ids fail loudly at the database
too. Database-generated ids (UUIDs, serials) are implementation
details and never replace protocol identity.

## JSONB usage

Only for genuinely schema-evolvable structured data: requested
capabilities, message envelopes, conversation/audit metadata. Never
for whole relational entities. JSONB columns round-trip exactly
(including nested structures and nulls).

## Transaction model

One short-lived session per unit of work via `session_scope`
(commit on success, rollback on error). Multi-step writes never
partially commit (tested). Repositories roll back before raising on a
failed flush, so the caller's session stays usable. `get_for_update`
offers row-level locking for future concurrent workers; unique
constraints make conflicting inserts fail safely with `ConflictError`.

## Repository model

Focused data access per aggregate (`create`/`get`/`list`/`update`/
 narrowly-scoped verbs like `deactivate`, `revoke`,
`save_response_reference`). Field updates accept stored columns only —
legality of any change is the caller's (later: Component 5's) policy,
never the repository's. Failures map to `PersistenceError`,
`ConflictError` (duplicates), or `DatabaseConnectionError`, chained
to the original for internal diagnostics.

## Migration strategy

Alembic is authoritative: `alembic upgrade head` creates the schema
(`c4init0001`), `alembic downgrade -1`/`base` reverses it.
`Base.metadata.create_all()` is never the production path. URL
resolution: `alembic -x url=…` > `ALEMBIC_DATABASE_URL` >
`NEXUS_DATABASE_URL` > `alembic.ini` default.

## Environment configuration

Copy `.env.example` to `.env`. `NEXUS_DATABASE_URL` wins, else
`NEXUS_DB_HOST/PORT/USER/PASSWORD/NAME` (defaults match
`docker-compose.yml`). Tests use `NEXUS_TEST_DATABASE_URL` (default
`…/nexus_test` on the compose database) and create it if missing.
Never commit secrets.

## Local PostgreSQL setup

```bash
docker compose up -d        # postgres:16 on 127.0.0.1:5432
alembic upgrade head        # create schema
python -m pytest tests/     # full suite (persistence hits real PG)
```

## What is deliberately NOT stored

Private Ed25519 keys, passwords, OAuth/token/session secrets, Gateway
socket or presence state, approval/request policy, embeddings, full
sensitive tool results. The `agents` table has no private-key column
by construction (asserted in tests).

## Persistence failure behavior

Duplicate ids/relations/approvals → `ConflictError`; missing foreign
rows, bad CHECK values, oversized ids, unserializable values →
`PersistenceError` (chained); unreachable database →
`DatabaseConnectionError` at connectivity check. Failed transactions
roll back fully; concurrent same-key inserts resolve to exactly one
winner plus one `ConflictError`.
