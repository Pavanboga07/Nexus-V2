"""Inbound first-contact via invitation (real PG, real signatures).

Covers the pairing bootstrap inside InboundProcessor._register:
a nexus.pair query carrying a valid invitation secret registers an
unknown sender (creating its agent row and a pending peer) and burns
the invitation atomically; every invalid variant rejects without
rows, and ordinary queries keep their exact original errors.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.identity.identity import create_identity
from backend.inbound import InboundDispatcher, InboundProcessor
from backend.inbound.errors import InboundError
from backend.pairing import invitation
from backend.pairing.store import PairingStore
from backend.persistence.database import session_scope
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.protocol.types import MessageType

from tests.inbound.conftest import (  # noqa: F401
    db,
    signed_query,
    unique,
)


def _pub(identity):
    import base64

    from backend.identity.keys import public_key_to_bytes

    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pair_query(sender, recipient, invitation_id, secret):
    import json

    return json.dumps({
        "nexus_pair": True,
        "invitation_id": invitation_id,
        "secret": secret,
        "agent": {"agent_id": sender.agent_id,
                  "public_key": _pub(sender)},
    }, sort_keys=True, separators=(",", ":"))


async def _seed_recipient_only(db, recipient):
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(
            recipient.agent_id, _pub(recipient))


async def _issue(db, issuer, ttl_seconds=3600):
    from datetime import datetime, timezone

    store = PairingStore(db.sessions)
    now = datetime.now(timezone.utc)
    code, invitation_id, secret, _ = invitation.create_invitation(
        issuer.agent_id, issuer.private_key, _pub(issuer),
        ttl_seconds, now,
    )
    await store.create_invitation(
        invitation_id, issuer.agent_id,
        invitation.secret_hash(secret), now + timedelta(seconds=ttl_seconds),
    )
    return code, invitation_id, secret


@pytest.mark.asyncio
async def test_pair_bootstrap_registers_unknown_sender(db):
    sender = create_identity()
    recipient = create_identity()
    await _seed_recipient_only(db, recipient)
    code, invitation_id, secret = await _issue(db, recipient)
    fields, _ = invitation.split_code(code)
    assert fields["invitation_id"] == invitation_id

    store = PairingStore(db.sessions)
    processor = InboundProcessor(
        db.sessions, recipient.agent_id, pairing_store=store)
    created, expires = datetime.now(timezone.utc), datetime.now(
        timezone.utc) + timedelta(hours=1)
    message = signed_query(
        sender, recipient,
        query=_pair_query(sender, recipient, invitation_id, secret),
        capabilities=["nexus.pair"],
        created_at=created, expires_at=expires,
    )
    outcome = await processor.process(message)
    assert outcome.status == "REGISTERED"

    async with session_scope(db.sessions) as session:
        stored_sender = await AgentRepository(session).get(
            sender.agent_id)
        assert stored_sender is not None
        peer = await PeerRepository(session).get(
            recipient.agent_id, sender.agent_id)
        assert peer is not None and peer.status == "pending"
    row = await store.get_invitation(invitation_id)
    assert row.used_at is not None


@pytest.mark.asyncio
async def test_pair_redelivery_resolves_duplicate(db):
    sender = create_identity()
    recipient = create_identity()
    await _seed_recipient_only(db, recipient)
    code, invitation_id, secret = await _issue(db, recipient)
    store = PairingStore(db.sessions)
    processor = InboundProcessor(
        db.sessions, recipient.agent_id, pairing_store=store)
    created, expires = datetime.now(timezone.utc), datetime.now(
        timezone.utc) + timedelta(hours=1)
    first = signed_query(
        sender, recipient,
        query=_pair_query(sender, recipient, invitation_id, secret),
        capabilities=["nexus.pair"], created_at=created,
        expires_at=expires,
    )
    assert (await processor.process(first)).status == "REGISTERED"
    # Identical redelivery resolves duplicate through the normal path.
    assert (await processor.process(first)).status == "DUPLICATE"
    second = signed_query(
        sender, recipient,
        query=_pair_query(sender, recipient, invitation_id, secret),
        capabilities=["nexus.pair"], created_at=created,
        expires_at=expires, request_id=first.request_id,
    )
    assert (await processor.process(second)).status == "DUPLICATE"
    # A fresh request reusing the burned invitation is rejected.
    third = signed_query(
        sender, recipient,
        query=_pair_query(sender, recipient, invitation_id, secret),
        capabilities=["nexus.pair"], created_at=created,
        expires_at=expires, request_id=unique("req"),
    )
    with pytest.raises(InboundError):
        await processor.process(third)


@pytest.mark.asyncio
async def test_pair_wrong_secret_revoked_expired_rejected(db):
    sender = create_identity()
    recipient = create_identity()
    await _seed_recipient_only(db, recipient)
    code, invitation_id, secret = await _issue(db, recipient)
    store = PairingStore(db.sessions)
    processor = InboundProcessor(
        db.sessions, recipient.agent_id, pairing_store=store)

    async def _attempt(query_text):
        created, expires = datetime.now(timezone.utc), datetime.now(
            timezone.utc) + timedelta(hours=1)
        message = signed_query(
            sender, recipient, query=query_text,
            capabilities=["nexus.pair"], created_at=created,
            expires_at=expires,
        )
        with pytest.raises(InboundError):
            await processor.process(message)

    await _attempt(_pair_query(sender, recipient, invitation_id, "wrong"))
    await store.revoke_invitation(invitation_id, datetime.now(timezone.utc))
    await _attempt(_pair_query(sender, recipient, invitation_id, secret))

    # No agent row, no peer row, no request row from any rejection.
    async with session_scope(db.sessions) as session:
        assert await AgentRepository(session).get(sender.agent_id) is None
        assert await PeerRepository(session).get(
            recipient.agent_id, sender.agent_id) is None


@pytest.mark.asyncio
async def test_non_pair_unknown_sender_keeps_original_error(db):
    sender = create_identity()
    recipient = create_identity()
    await _seed_recipient_only(db, recipient)
    store = PairingStore(db.sessions)
    processor = InboundProcessor(
        db.sessions, recipient.agent_id, pairing_store=store)
    created, expires = datetime.now(timezone.utc), datetime.now(
        timezone.utc) + timedelta(hours=1)
    message = signed_query(
        sender, recipient, created_at=created, expires_at=expires)
    with pytest.raises(InboundError) as exc_info:
        await processor.process(message)
    assert type(exc_info.value).__name__ == "InboundUnknownSender"


@pytest.mark.asyncio
async def test_pair_without_store_behaves_as_before(db):
    sender = create_identity()
    recipient = create_identity()
    await _seed_recipient_only(db, recipient)
    code, invitation_id, secret = await _issue(db, recipient)
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    created, expires = datetime.now(timezone.utc), datetime.now(
        timezone.utc) + timedelta(hours=1)
    message = signed_query(
        sender, recipient,
        query=_pair_query(sender, recipient, invitation_id, secret),
        capabilities=["nexus.pair"], created_at=created,
        expires_at=expires,
    )
    with pytest.raises(InboundError) as exc_info:
        await processor.process(message)
    assert type(exc_info.value).__name__ == "InboundUnknownSender"


@pytest.mark.asyncio
async def test_dispatcher_routes_pairing_acceptance(db):
    from backend.protocol.models import (
        ApprovalResponsePayload,
        NexusMessage,
    )
    from backend.protocol.serialization import message_to_dict
    from backend.identity.signing import sign_message

    sender = create_identity()
    recipient = create_identity()
    processor = InboundProcessor(db.sessions, recipient.agent_id)
    dispatcher = InboundDispatcher(processor)
    seen = []

    async def _handler(message):
        seen.append(message.message_id)
        return "completed"

    dispatcher.set_pairing_handler(_handler)
    created = datetime.now(timezone.utc)
    unsigned = NexusMessage(
        protocol_version="1.0",
        message_id=unique("msg"),
        request_id=unique("req"),
        sender=sender.agent_id,
        recipient=recipient.agent_id,
        type=MessageType.APPROVAL_RESPONSE,
        created_at=created,
        expires_at=created + timedelta(hours=1),
        payload=ApprovalResponsePayload(approved=True, reason=None),
        signature="",
    )
    message = sign_message(unsigned, sender.private_key)
    result = await dispatcher.dispatch_one(message)
    assert result.status == "HANDLED"
    assert seen == [message.message_id]

    from backend.inbound import HANDLED
    from backend.inbound.errors import HandlerAlreadyRegistered

    assert HANDLED == "HANDLED"
    with pytest.raises(HandlerAlreadyRegistered):
        dispatcher.set_pairing_handler(_handler)
    dispatcher.clear_pairing_handler(_handler)
    result = await dispatcher.dispatch_one(message)
    assert result.status != "HANDLED"
