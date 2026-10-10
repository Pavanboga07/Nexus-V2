"""Shared fixtures for Component 11 tests (isolated real PostgreSQL).

Models the originating side: the local agent creates an outbound
request, the worker hands it to the Gateway (QUEUED -> SENT), and a
signed REMOTE_RESPONSE later arrives from the remote agent.
"""

from __future__ import annotations

import asyncio
import base64
import os
import pathlib
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_engine,
)
from sqlalchemy.pool import NullPool

from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.identity.signing import sign_message
from backend.persistence.database import (
    create_session_factory,
    dispose_engine,
    ensure_database_exists,
    session_scope,
)
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.protocol.models import NexusMessage, RemoteResponsePayload
from backend.protocol.serialization import message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.requests.service import RequestService
from backend.responses import ResponseReceiver

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TEST_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus_test_responses",
)


def _migrate():
    asyncio.run(ensure_database_exists(TEST_URL))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def db():
    _migrate()
    engine = _sa_create_engine(
        TEST_URL, poolclass=NullPool, pool_pre_ping=True
    )
    try:
        yield types.SimpleNamespace(
            engine=engine, sessions=create_session_factory(engine)
        )
    finally:
        asyncio.run(dispose_engine(engine))


class Keys:
    """Identity provider stub holding private keys."""

    def __init__(self):
        self._keys = {}

    def add(self, identity):
        self._keys[identity.agent_id] = identity.private_key
        return identity

    def private_key_for(self, agent_id):
        return self._keys[agent_id]


def _pub(identity):
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unique(prefix):
    return f"{prefix}11_{uuid.uuid4().hex}"


def live_times(ttl_hours=1):
    created = datetime.now(timezone.utc)
    return created, created + timedelta(hours=ttl_hours)


async def seed_originator_sent(db, keys, query="What is two plus two?",
                               conversation_id=None,
                               originator=None, remote=None):
    """Originator creates a request; the worker hands it off (SENT).

    Returns (originator, remote, request_id) with durable dispatch
    evidence: request SENT, outbox SENT, outbound query persisted.
    Pass existing identities to reuse them (e.g. a thread owner).
    """
    from backend.persistence.repositories.messages import MessageRepository
    from backend.persistence.repositories.outbox import OutboxRepository
    from backend.worker.config import WorkerConfig
    from backend.worker.outbox_dispatcher import OutboxDispatcher

    originator = originator or keys.add(create_identity())
    remote = remote or keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        if await AgentRepository(session).get(originator.agent_id) is None:
            await AgentRepository(session).create(
                originator.agent_id, _pub(originator))
        if await AgentRepository(session).get(remote.agent_id) is None:
            await AgentRepository(session).create(
                remote.agent_id, _pub(remote))
        if await PeerRepository(session).get(
            originator.agent_id, remote.agent_id
        ) is None:
            await PeerRepository(session).create(
                originator.agent_id, remote.agent_id, status="connected"
            )
    created, expires = live_times()
    service = RequestService(db.sessions, keys)
    record = await service.create_remote_request(
        originator.agent_id, remote.agent_id, query, [],
        expires, now=created, conversation_id=conversation_id,
    )
    assert record.status == "QUEUED"

    sent = {}

    class FakeClient:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.state = "CONNECTED"

        async def ensure_connected(self):
            return None

        async def send_message(self, envelope, timeout=None):
            from backend.gateway_client.models import AckResult as Ack

            sent[envelope["message_id"]] = dict(envelope)
            return Ack(envelope["message_id"], "DELIVERED", "delivered",
                       envelope.get("request_id", ""))

        async def close(self):
            self.state = "CLOSED"

    worker = OutboxDispatcher(
        sessions=db.sessions,
        identities=keys,
        gateway_url="ws://test",
        service=RequestService(db.sessions, keys),
        config=WorkerConfig(),
        client_factory=lambda sender_id: FakeClient(sender_id),
        clock=lambda: datetime.now(timezone.utc),
        jitter=lambda: 0.0,
    )
    results = await worker.run_once(batch_size=100)
    ours = [r for r in results if r.request_id == record.request_id]
    assert len(ours) == 1
    assert ours[0].outcome.value == "SENT"
    stored = await service.get_request(record.request_id)
    assert stored is not None and stored.status == "SENT"
    async with session_scope(db.sessions) as session:
        outbox_rows = [
            await OutboxRepository(session).get(message_id)
            for message_id in sent
        ]
    assert all(row is not None and row.status == "SENT"
               for row in outbox_rows)
    return originator, remote, record.request_id


def signed_response(responder, requester_id, request_id,
                    message_id=None, created=None, expires=None,
                    payload_over=None):
    """Build and sign a REMOTE_RESPONSE exactly like Component 10."""
    now = created or datetime.now(timezone.utc)
    payload = RemoteResponsePayload(status="ok", answer="four")
    for name, value in (payload_over or {}).items():
        setattr(payload, name, value)
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id or unique("msg"),
        request_id=request_id,
        sender=responder.agent_id,
        recipient=requester_id,
        type=MessageType.REMOTE_RESPONSE,
        created_at=now,
        expires_at=(expires if expires is not None
                    else now + timedelta(hours=1)),
        payload=payload,
        signature="",
    )
    return sign_message(message, responder.private_key)


def make_receiver(db, keys, local_agent_id):
    return ResponseReceiver(db.sessions, local_agent_id, keys)


async def response_messages(db, request_id):
    """Stored REMOTE_RESPONSE rows for a request."""
    from backend.persistence.repositories.messages import MessageRepository

    async with session_scope(db.sessions) as session:
        rows = await MessageRepository(session).list_by_request(request_id)
        return [row for row in rows
                if row.message_type == "REMOTE_RESPONSE"]
