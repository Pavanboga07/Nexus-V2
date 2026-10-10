"""Pairing orchestration: invitations, acceptance, completion.

Trust model: an invitation proves the issuer authorized first contact
(signed envelope) and that the presenter holds the out-of-band secret
(single-use bearer credential). Nothing here invents trust: agent rows
and peer rows are created only after one of those two proofs checks
out, and B-side completion additionally requires B's explicit
approval of the pairing request through the standard path.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.gateway_client.errors import (
    AckTimeoutError,
    ConnectionLostError,
    MessageTooLargeError,
    NotConnectedError,
)
from backend.identity.keys import public_key_to_bytes
from backend.identity.signing import sign_message
from backend.identity.verification import verify_message
from backend.persistence.database import session_scope
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.protocol.models import (
    ApprovalResponsePayload,
    NexusMessage,
)
from backend.protocol.serialization import message_to_dict
from backend.protocol.types import (
    PROTOCOL_VERSION,
    MessageType,
    generate_message_id,
)
from backend.protocol.validation import validate_message
from backend.requests.errors import RetryableInfrastructureError
from backend.requests.service import RequestService

from . import invitation
from .errors import (
    PairingConflictError,
    PairingInvalidError,
    PairingNotFoundError,
    PairingTransportError,
)
from .store import PairingStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pub_b64(private_key: Any) -> str:
    import base64

    raw = public_key_to_bytes(private_key.public_key())
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class PairingService:
    """One agent's pairing operations (issuer and redeemer sides)."""

    def __init__(
        self,
        sessions: Any,
        local_agent_id: str,
        identities: Any,
        store: PairingStore | None = None,
        on_event: Any = None,
    ) -> None:
        self._sessions = sessions
        self._local_agent_id = local_agent_id
        self._identities = identities
        self._store = store or PairingStore(sessions)
        self._on_event = on_event

    # -- issuer side (User B) --------------------------------------------------

    async def create_invitation(
        self,
        issuer_agent_id: str,
        ttl_seconds: int = 86400,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Issue one invitation; the secret is returned only here."""
        current = now or _utcnow()
        try:
            private_key = self._identities.private_key_for(
                issuer_agent_id)
        except KeyError as exc:
            raise PairingInvalidError(
                "no signing identity for this agent") from exc
        public_b64 = _pub_b64(private_key)
        code, invitation_id, secret, expires_at = invitation.create_invitation(
            issuer_agent_id, private_key, public_b64, ttl_seconds,
            current,
        )
        await self._store.create_invitation(
            invitation_id, issuer_agent_id,
            invitation.secret_hash(secret), expires_at,
        )
        return {
            "invitation_id": invitation_id,
            "invitation": code,
            "secret": secret,
            "expires_at": expires_at.isoformat(),
        }

    # -- redeemer side (User A) --------------------------------------------------

    @staticmethod
    def pairing_query_for(code_fields: dict[str, Any], secret: str,
                          agent_id: str, public_key: str) -> str:
        """Canonical pairing handshake payload carried as query text."""
        return json.dumps({
            "nexus_pair": True,
            "invitation_id": code_fields["invitation_id"],
            "secret": secret,
            "agent": {"agent_id": agent_id, "public_key": public_key},
        }, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def parse_pairing_query(query: Any) -> dict[str, Any] | None:
        """Strict pairing payload or None (never raises, never trusts)."""
        if not isinstance(query, str):
            return None
        try:
            payload = json.loads(query)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("nexus_pair") is not True:
            return None
        agent = payload.get("agent")
        if not isinstance(agent, dict):
            return None
        for name in ("invitation_id", "secret"):
            if not isinstance(payload.get(name), str):
                return None
        for name in ("agent_id", "public_key"):
            if not isinstance(agent.get(name), str):
                return None
        return payload

    async def accept_invitation(
        self,
        code: str,
        sender_agent_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify an invitation and send the pairing request.

        Creates the verified remote agent row and a connected peer row
        (the issuer's signature is first-contact authorization), then
        submits a normal pairing-flagged request through RequestService
        so delivery, registration, and approval all use the standard
        path. Idempotent for an already-open link.
        """
        from backend.identity.identity import create_identity  # noqa: F401
        current = now or _utcnow()
        fields, secret = invitation.split_code(code)
        if sender_agent_id == fields["agent_id"]:
            raise PairingInvalidError(
                "an agent cannot pair with itself")
        async with session_scope(self._sessions) as session:
            agents = AgentRepository(session)
            sender = await agents.get(sender_agent_id)
            if sender is None or not sender.active:
                raise PairingNotFoundError(
                    "sending agent is unknown or inactive")
            if await agents.get(fields["agent_id"]) is None:
                await agents.create(
                    fields["agent_id"], fields["public_key"])
        existing = await self._store.find_open_link(
            sender_agent_id, fields["agent_id"])
        if existing is not None:
            return {
                "pairing_id": str(existing.id),
                "request_id": existing.request_id,
                "remote_agent_id": fields["agent_id"],
                "invitation_id": fields["invitation_id"],
                "status": existing.status,
                "duplicate": True,
            }
        async with session_scope(self._sessions) as session:
            peers = PeerRepository(session)
            peer = await peers.get(sender_agent_id, fields["agent_id"])
            if peer is None:
                await peers.create(
                    sender_agent_id, fields["agent_id"],
                    status="connected")
            elif peer.status != "connected":
                await peers.update(
                    sender_agent_id, fields["agent_id"],
                    status="connected")
        query = self.pairing_query_for(
            fields, secret, sender_agent_id,
            _pub_b64(self._key_for(sender_agent_id)),
        )
        try:
            expires_at = datetime.fromisoformat(fields["expires_at"])
        except ValueError as exc:
            raise PairingInvalidError(
                "invitation expiry is malformed") from exc
        service = RequestService(self._sessions, self._identities)
        record = await service.create_remote_request(
            sender_agent_id,
            fields["agent_id"],
            query,
            [invitation.PAIR_CAPABILITY],
            min(expires_at, current + timedelta(days=7)),
            now=current,
        )
        link = await self._store.create_link(
            sender_agent_id, fields["agent_id"], fields["public_key"],
            invitation_id=fields["invitation_id"],
            request_id=record.request_id,
        )
        self._emit("remote.pairing.updated", {
            "pairing_id": str(link.id),
            "request_id": record.request_id,
            "status": "pending",
        })
        return {
            "pairing_id": str(link.id),
            "request_id": record.request_id,
            "remote_agent_id": fields["agent_id"],
            "invitation_id": fields["invitation_id"],
            "status": "pending",
            "duplicate": False,
        }

    def _key_for(self, agent_id: str):
        try:
            return self._identities.private_key_for(agent_id)
        except KeyError as exc:
            raise PairingInvalidError(
                "no signing identity for this agent") from exc

    # -- completion side (User B, after explicit approval) -----------------------

    async def complete_pairing(
        self,
        request_id: str,
        local_agent_id: str,
        client: Any,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Send the pairing acceptance for a decided pairing request.

        The decision comes from persisted request state (APPROVED or
        REJECTED via the standard endpoints) — never from a client
        claim. Idempotent: repeating it re-sends without duplicating
        rows; peer creation is create-or-update.
        """
        current = now or _utcnow()
        service = RequestService(self._sessions, self._identities)
        record = await service.get_request(request_id)
        if record is None:
            raise PairingNotFoundError("request not found")
        if record.recipient_agent_id != local_agent_id:
            raise PairingInvalidError(
                "only the recipient side completes a pairing")
        if invitation.PAIR_CAPABILITY not in list(
            record.requested_capabilities or []
        ):
            raise PairingInvalidError("request is not a pairing request")
        if record.status not in ("APPROVED", "REJECTED"):
            raise PairingConflictError(
                "pairing completes only after an approval decision")
        approved = record.status == "APPROVED"
        parsed = self.parse_pairing_query(record.query)
        if parsed is None:
            raise PairingInvalidError(
                "pairing request carries no valid handshake")
        invitation_row = await self._store.get_invitation(
            parsed["invitation_id"])
        if invitation_row is None:
            raise PairingNotFoundError("invitation is unknown")
        if invitation_row.revoked_at is not None:
            raise PairingInvalidError("invitation was revoked")
        expires_at = invitation_row.expires_at
        if expires_at.tzinfo is None or current >= expires_at:
            raise PairingInvalidError("invitation has expired")
        async with session_scope(self._sessions) as session:
            peers = PeerRepository(session)
            peer = await peers.get(local_agent_id, record.sender_agent_id)
            if peer is None:
                await peers.create(
                    local_agent_id, record.sender_agent_id,
                    status="connected")
            elif peer.status != "connected":
                await peers.update(
                    local_agent_id, record.sender_agent_id,
                    status="connected")
        message = NexusMessage(
            protocol_version=PROTOCOL_VERSION,
            message_id=generate_message_id(),
            request_id=request_id,
            sender=local_agent_id,
            recipient=record.sender_agent_id,
            type=MessageType.APPROVAL_RESPONSE,
            created_at=current,
            expires_at=min(
                record.expires_at,
                current + timedelta(days=7),
            ) if record.expires_at.tzinfo is not None else current,
            payload=ApprovalResponsePayload(approved=approved, reason=None),
            signature="",
        )
        try:
            signed = sign_message(
                message, self._key_for(local_agent_id))
            envelope = message_to_dict(signed)
        except PairingInvalidError:
            raise
        except Exception as exc:
            raise PairingInvalidError(
                "acceptance message could not be signed") from exc
        try:
            ack = await client.send_message(envelope)
        except (AckTimeoutError, ConnectionLostError,
                NotConnectedError) as exc:
            raise PairingTransportError(
                "the pairing acceptance could not be delivered; "
                "it is safe to retry") from exc
        except MessageTooLargeError as exc:
            raise PairingInvalidError(
                "the pairing acceptance exceeds transport limits"
            ) from exc
        if ack.status not in ("DELIVERED", "BUFFERED"):
            raise PairingTransportError(
                f"the gateway refused delivery: {ack.detail or ack.status}")
        link = await self._store.create_link(
            local_agent_id, record.sender_agent_id,
            parsed["agent"]["public_key"],
            invitation_id=parsed["invitation_id"],
            request_id=request_id,
        )
        final = "completed" if approved else "rejected"
        await self._store.set_link_status(link.id, final)
        self._emit("remote.pairing.updated", {
            "pairing_id": str(link.id),
            "request_id": request_id,
            "status": final,
        })
        return {
            "pairing_id": str(link.id),
            "approved": approved,
            "message_id": envelope["message_id"],
            "ack": ack.status,
        }

    # -- acceptance path (User A, async via dispatcher) --------------------------

    async def handle_acceptance(self, message: Any) -> str:
        """Consume one APPROVAL_RESPONSE for a pending pairing link.

        Returns a short outcome string for the dispatcher. Unknown or
        already-settled links resolve as ignored (idempotent); invalid
        signatures and bindings raise.
        """
        if not isinstance(message, NexusMessage):
            raise PairingInvalidError("not a protocol message")
        if message.type is not MessageType.APPROVAL_RESPONSE:
            raise PairingInvalidError("not a pairing acceptance")
        try:
            link = await self._find_link_for(message)
        except (OperationalError, SQLTimeoutError, OSError) as exc:
            raise RetryableInfrastructureError(
                "pairing store is temporarily unavailable") from exc
        if link is None:
            return "ignored-unknown"
        if link.status != "pending":
            return "ignored-duplicate"
        try:
            _key, _raw = invitation.decode_public_key(
                link.remote_public_key)
            public_key = _key
        except PairingInvalidError as exc:
            raise PairingInvalidError(
                "stored peer key is invalid") from exc
        if message.sender != link.remote_agent_id:
            raise PairingInvalidError(
                "acceptance sender does not match the pairing")
        try:
            verify_message(message, public_key, now=_utcnow())
        except Exception as exc:
            raise PairingInvalidError(
                "acceptance signature is invalid") from exc
        approved = bool(getattr(message.payload, "approved", False))
        final = "completed" if approved else "rejected"
        try:
            if approved:
                async with session_scope(self._sessions) as session:
                    peers = PeerRepository(session)
                    peer = await peers.get(
                        link.local_agent_id, link.remote_agent_id)
                    if peer is None:
                        await peers.create(
                            link.local_agent_id, link.remote_agent_id,
                            status="connected")
                    elif peer.status != "connected":
                        await peers.update(
                            link.local_agent_id, link.remote_agent_id,
                            status="connected")
            await self._store.set_link_status(link.id, final)
        except (OperationalError, SQLTimeoutError, OSError) as exc:
            raise RetryableInfrastructureError(
                "pairing store is temporarily unavailable") from exc
        self._emit("remote.pairing.updated", {
            "pairing_id": str(link.id),
            "request_id": link.request_id,
            "status": final,
        })
        return final

    async def _find_link_for(self, message: Any):
        links = await self._store.list_links(self._local_agent_id)
        for link in links:
            if (link.request_id is not None
                    and link.request_id == message.request_id):
                return link
        return None

    async def attach(self, dispatcher: Any) -> None:
        """Register on a dispatcher, then process retained acceptances.

        Registration precedes the backlog drain so acceptances arriving
        mid-attach route to the handler while only pre-registration
        backlog is drained here. Retained non-acceptance messages are
        re-queued in order.
        """
        dispatcher.set_pairing_handler(self.handle_acceptance)
        await self._drain_backlog(dispatcher)

    async def detach(self, dispatcher: Any) -> None:
        """Unregister from a dispatcher; retained mail stays retained."""
        dispatcher.clear_pairing_handler(self.handle_acceptance)

    async def _drain_backlog(self, dispatcher: Any) -> None:
        """Process pre-registration acceptances; preserve other mail."""
        backlog: list[Any] = []
        while True:
            try:
                backlog.append(dispatcher.unhandled.get_nowait())
            except asyncio.QueueEmpty:
                break
        others = [
            item for item in backlog
            if not (
                isinstance(item, NexusMessage)
                and item.type is MessageType.APPROVAL_RESPONSE
            )
        ]
        for item in others:
            await dispatcher.unhandled.put(item)
        for item in backlog:
            if (
                isinstance(item, NexusMessage)
                and item.type is MessageType.APPROVAL_RESPONSE
            ):
                await self.handle_acceptance(item)

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event_type, data)
            except Exception:
                pass
