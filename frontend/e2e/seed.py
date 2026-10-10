"""Seed the frontend E2E database (dev tooling, not part of the app).

Creates, for the owner in E2E_OWNER_USER_ID:
- one owned local agent with a known remote peer (connected),
- one inbound WAITING_APPROVAL request from that peer.

Idempotent: skips work when the marker agent already exists.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from backend.identity.identity import create_identity  # noqa: E402
from backend.identity.keys import public_key_to_bytes  # noqa: E402
from backend.persistence.database import (  # noqa: E402
    create_session_factory,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.models import User  # noqa: E402
from backend.persistence.repositories.agents import AgentRepository  # noqa: E402
from backend.persistence.repositories.peers import PeerRepository  # noqa: E402

E2E_DB_URL = os.environ.get(
    "NEXUS_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_frontend_e2e",
)
OWNER = uuid.UUID(os.environ["E2E_OWNER_USER_ID"])
KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".keys.json")

_TABLES = [
    "conversation_messages",
    "conversations",
    "messages",
    "outbox_messages",
    "approvals",
    "remote_requests",
    "agent_peers",
    "agents",
    "users",
    "audit_events",
]


def _pub(identity) -> str:
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


async def _stable_local_agent(sessions):
    """Reuse the seeded local agent (and its keys file) when present.

    Rotating the agent every run would invalidate the running backend's
    in-memory keys; stability lets the backend stay up across reseeds.
    """
    from backend.identity.keys import private_key_to_bytes

    try:
        with open(KEYS_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict) and len(saved) == 1:
        [[agent_id]] = [[key] for key in saved.keys()]
        async with session_scope(sessions) as session:
            row = await AgentRepository(session).get(agent_id)
            if row is not None and row.owner_user_id == OWNER:
                print(f"e2e seed: reusing local agent ({agent_id})")
                return _KnownAgent(agent_id)
    identity = create_identity()
    async with session_scope(sessions) as session:
        await AgentRepository(session).create(
            identity.agent_id, _pub(identity), owner_user_id=OWNER
        )
    raw_key = private_key_to_bytes(identity.private_key)
    with open(KEYS_FILE, "w", encoding="utf-8") as handle:
        json.dump(
            {identity.agent_id: base64.b64encode(raw_key).decode("ascii")},
            handle,
        )
    try:
        os.chmod(KEYS_FILE, 0o600)
    except OSError:
        pass
    print(f"e2e seed: created local agent ({identity.agent_id})")
    return identity


class _KnownAgent:
    """Identity handle for a reused agent (public id only)."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


async def main() -> None:
    await ensure_database_exists(E2E_DB_URL)

    engine = create_async_engine(E2E_DB_URL, poolclass=NullPool, pool_pre_ping=True)
    sessions = create_session_factory(engine)
    try:
        # Dedicated E2E database: deterministic run. Request-scoped tables
        # are wiped; the local agent is STABLE across runs (its private key
        # lives in .keys.json, which the backend reads once at startup, so
        # rotating the agent would orphan the running backend's keys).
        async with engine.begin() as connection:
            from sqlalchemy import text as _text

            await connection.execute(
                _text(
                    'TRUNCATE "conversation_messages", "conversations", '
                    '"messages", "outbox_messages", "approvals", '
                    '"remote_requests", "audit_events" CASCADE'
                )
            )

        from backend.persistence.repositories.users import UserRepository

        async with session_scope(sessions) as session:
            if await UserRepository(session).get(OWNER) is None:
                session.add(User(id=OWNER))

        local = await _stable_local_agent(sessions)

        # A fresh remote peer every run: inbound messages must be signed,
        # and only a newly created identity has its private key here.
        from backend.identity.signing import sign_message
        from backend.inbound import InboundProcessor
        from backend.requests.message_builder import build_remote_query_message

        remote = create_identity()
        async with session_scope(sessions) as session:
            await AgentRepository(session).create(remote.agent_id, _pub(remote))
            await PeerRepository(session).create(
                local.agent_id, remote.agent_id, status="connected"
            )

        created = datetime.now(timezone.utc)
        unsigned = build_remote_query_message(
            sender_agent_id=remote.agent_id,
            recipient_agent_id=local.agent_id,
            request_id=f"e2e-req-{uuid.uuid4().hex[:8]}",
            message_id=f"e2e-msg-{uuid.uuid4().hex[:8]}",
            query="E2E approval probe: summarize the release notes.",
            requested_capabilities=[],
            created_at=created,
            expires_at=created + timedelta(hours=1),
        )
        message = sign_message(unsigned, remote.private_key)
        outcome = await InboundProcessor(sessions, local.agent_id).process(message)
        assert outcome.status == "REGISTERED"
        print(f"e2e seed: local={local.agent_id} remote={remote.agent_id} req={message.request_id}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import subprocess

    # alembic's env.py drives its own event loop; migrate out-of-process.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
    )
    asyncio.run(main())
