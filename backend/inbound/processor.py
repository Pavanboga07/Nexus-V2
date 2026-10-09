"""Inbound message processor: verify, then durably register for approval.

One ``InboundProcessor`` serves exactly one local agent id. It accepts
an already-parsed :class:`NexusMessage` of type ``REMOTE_QUERY``,
authenticates it against the stored sender key and peer record, and —
inside a single database transaction — persists the recipient-side
request (``WAITING_APPROVAL``), the received signed message, and an
audit event. It never executes the requested task, never approves, and
never touches the network.

Idempotency is enforced by database uniqueness (``remote_requests``
and ``messages`` primary keys), never by memory alone: a duplicate
delivery returns an explicit :data:`DUPLICATE` outcome, a conflicting
reuse raises without overwriting, and concurrent deliveries resolve to
exactly one registration.
"""

from __future__ import annotations

import base64
import binascii
import functools
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)
from sqlalchemy.exc import OperationalError, TimeoutError as SQLTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.errors import (
    ExpiredMessageError,
    IdentityError,
    InvalidSignatureError,
    MalformedSignatureError,
    ReplayDetectedError,
    SenderMismatchError,
)
from backend.identity.verification import verify_message
from backend.persistence.database import session_scope
from backend.persistence.errors import ConflictError
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.audit_events import AuditEventRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.serialization import (
    ProtocolSerializationError,
    message_to_dict,
)
from backend.protocol.types import MessageType, is_valid_capability
from backend.protocol.validation import ProtocolValidationError
from backend.requests.models import RequestRecord
from backend.requests.service import MAX_CAPABILITIES, MAX_QUERY_LENGTH
from backend.requests.states import WAITING_APPROVAL

from .errors import (
    InboundConflictingDuplicate,
    InboundError,
    InboundExpired,
    InboundInactiveSender,
    InboundInfrastructureError,
    InboundInvalidEnvelope,
    InboundInvalidPayload,
    InboundInvalidSignature,
    InboundPeerError,
    InboundSenderKeyError,
    InboundUnknownSender,
    InboundUnsupportedType,
    InboundWrongRecipient,
)
from .models import DUPLICATE, REGISTERED, InboundOutcome

#: Audit fact appended when a request is first registered.
EVENT_REGISTERED = "inbound_request_registered"


def _translate_infrastructure_errors(fn):
    """Map connectivity-level failures to a retryable typed error.

    Mirrors the Request Service classifier: only lost/refused
    connections, pool timeouts, and socket errors become
    :class:`InboundInfrastructureError`. Domain failures pass through
    untouched so conflicts and rejections can never look retryable.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (OperationalError, SQLTimeoutError, OSError) as exc:
            raise InboundInfrastructureError(
                f"transient infrastructure failure in {fn.__name__}"
            ) from exc

    return wrapper


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: Any, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InboundInvalidEnvelope(f"{name} must be timezone-aware")
    return value


class InboundProcessor:
    """Verify inbound REMOTE_QUERY messages and register them durably."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        local_agent_id: str,
        replay_registry: Any | None = None,
    ) -> None:
        """Bind the processor to one local agent id.

        ``replay_registry`` is optional and append-only: a message id
        is recorded there only after its registration commits, so a
        crash before commit can never poison a legitimate retry. It is
        never consulted to accept or reject; the messages table is the
        durable replay record and survives restarts.
        """
        self._sessions = sessions
        self._local_agent_id = local_agent_id
        self._replay = replay_registry

    @property
    def local_agent_id(self) -> str:
        """The agent id this processor receives for."""
        return self._local_agent_id

    @_translate_infrastructure_errors
    async def process(
        self, message: NexusMessage, *, now: datetime | None = None
    ) -> InboundOutcome:
        """Verify one message and register its request for approval.

        Returns :data:`REGISTERED` on first registration and
        :data:`DUPLICATE` when the same logical request was already
        registered. Raises a typed :class:`InboundError` otherwise;
        rejected messages leave no request, message, or audit rows.
        """
        current = _require_aware(now, "now") if now is not None else _utcnow()
        self._check_routable(message)
        try:
            async with session_scope(self._sessions) as session:
                outcome = await self._register(session, message, current)
        except ConflictError:
            return await self._classify_conflict(message)
        if self._replay is not None:
            try:
                self._replay.record(message.message_id, message.expires_at)
            except Exception:  # noqa: BLE001 - advisory only, never fails
                pass
        return outcome

    # -- validation ------------------------------------------------------

    def _check_routable(self, message: NexusMessage) -> RemoteQueryPayload:
        """Cheap pre-database checks (type, recipient, payload policy)."""
        if not isinstance(message, NexusMessage):
            raise InboundInvalidEnvelope("message must be a NexusMessage")
        if message.type is not MessageType.REMOTE_QUERY:
            raise InboundUnsupportedType(
                f"unsupported message type: {message.type!r}"
            )
        if message.recipient != self._local_agent_id:
            raise InboundWrongRecipient(
                f"message {message.message_id!r} is not addressed to "
                "this agent"
            )
        payload = message.payload
        if not isinstance(payload, RemoteQueryPayload):
            raise InboundInvalidEnvelope(
                f"message {message.message_id!r} has no remote-query payload"
            )
        if not isinstance(payload.query, str) or not payload.query.strip():
            raise InboundInvalidPayload(
                f"message {message.message_id!r} has an empty query"
            )
        if len(payload.query) > MAX_QUERY_LENGTH:
            raise InboundInvalidPayload(
                f"message {message.message_id!r} query exceeds "
                f"{MAX_QUERY_LENGTH} characters"
            )
        if not isinstance(payload.requested_capabilities, list):
            raise InboundInvalidPayload(
                f"message {message.message_id!r} capabilities must be a list"
            )
        if len(payload.requested_capabilities) > MAX_CAPABILITIES:
            raise InboundInvalidPayload(
                f"message {message.message_id!r} requests more than "
                f"{MAX_CAPABILITIES} capabilities"
            )
        for capability in payload.requested_capabilities:
            if not is_valid_capability(capability):
                raise InboundInvalidPayload(
                    f"message {message.message_id!r} has an invalid "
                    "requested capability"
                )
        return payload

    # -- registration ----------------------------------------------------

    async def _register(
        self,
        session: AsyncSession,
        message: NexusMessage,
        current: datetime,
    ) -> InboundOutcome:
        """Run authentication and the atomic insert set (one transaction)."""
        agents = AgentRepository(session)
        recipient = await agents.get(message.recipient)
        if recipient is None or not recipient.active:
            # The recipient row mirrors the local agent; without an
            # active row this agent cannot take custody of the request.
            raise InboundWrongRecipient(
                f"message {message.message_id!r} is not addressed to "
                "an active local agent"
            )
        sender = await agents.get(message.sender)
        if sender is None:
            raise InboundUnknownSender(
                f"unknown sender {message.sender!r}"
            )
        if not sender.active:
            raise InboundInactiveSender(
                f"sender {message.sender!r} is deactivated"
            )
        public_key = self._sender_public_key(message, sender)
        peer = await PeerRepository(session).get(
            self._local_agent_id, message.sender
        )
        if peer is None or peer.status != "connected":
            raise InboundPeerError(
                f"no connected peer relationship {message.sender!r} "
                f"-> {self._local_agent_id!r}"
            )
        try:
            verify_message(message, public_key, now=current)
        except ReplayDetectedError:
            # No registry is passed, so this cannot trigger; map it to
            # the idempotent outcome rather than a rejection if it ever
            # does (e.g. a future verification default changes).
            return await self._duplicate_outcome(session, message)
        except ExpiredMessageError as exc:
            raise InboundExpired(str(exc)) from exc
        except (InvalidSignatureError, MalformedSignatureError) as exc:
            raise InboundInvalidSignature(str(exc)) from exc
        except SenderMismatchError as exc:
            raise InboundSenderKeyError(str(exc)) from exc
        except (IdentityError, ProtocolValidationError,
                ProtocolSerializationError) as exc:
            raise InboundInvalidEnvelope(str(exc)) from exc
        payload = message.payload
        assert isinstance(payload, RemoteQueryPayload)
        wire = message_to_dict(message)
        envelope = {
            key: value for key, value in wire.items() if key != "signature"
        }
        existing_request = await RemoteRequestRepository(session).get(
            message.request_id
        )
        if existing_request is not None:
            if self._same_request(existing_request, message):
                return await self._duplicate_outcome(session, message)
            raise InboundConflictingDuplicate(
                f"request {message.request_id!r} already registered "
                "with different content"
            )
        existing_message = await MessageRepository(session).get(
            message.message_id
        )
        if existing_message is not None:
            if self._same_message(existing_message, message, envelope):
                return await self._duplicate_outcome(session, message)
            raise InboundConflictingDuplicate(
                f"message {message.message_id!r} already stored "
                "with different content"
            )
        row = await RemoteRequestRepository(session).create(
            request_id=message.request_id,
            sender_agent_id=message.sender,
            recipient_agent_id=message.recipient,
            query=payload.query,
            requested_capabilities=list(payload.requested_capabilities),
            status=WAITING_APPROVAL,
            created_at=message.created_at,
            expires_at=message.expires_at,
        )
        await MessageRepository(session).create(
            message_id=message.message_id,
            request_id=message.request_id,
            sender_agent_id=message.sender,
            recipient_agent_id=message.recipient,
            message_type=message.type.value,
            protocol_version=message.protocol_version,
            created_at=message.created_at,
            expires_at=message.expires_at,
            signature=message.signature,
            envelope=envelope,
        )
        await AuditEventRepository(session).create(
            EVENT_REGISTERED,
            actor_agent_id=message.sender,
            request_id=message.request_id,
            message_id=message.message_id,
            meta={
                "message_type": message.type.value,
                "protocol_version": message.protocol_version,
                "capability_count": len(payload.requested_capabilities),
            },
        )
        return InboundOutcome(
            status=REGISTERED,
            request_id=row.request_id,
            message_id=message.message_id,
            record=RequestRecord.from_row(row, message_id=message.message_id),
        )

    def _sender_public_key(self, message: NexusMessage, sender: Any):
        """Decode the stored sender key (never trust the wire for this)."""
        if sender.key_algorithm != "Ed25519":
            raise InboundSenderKeyError(
                f"sender {message.sender!r} uses unsupported key "
                f"algorithm {sender.key_algorithm!r}"
            )
        try:
            raw = base64.urlsafe_b64decode(
                sender.public_key + "=" * (-len(sender.public_key) % 4)
            )
            return Ed25519PublicKey.from_public_bytes(raw)
        except (binascii.Error, ValueError) as exc:
            raise InboundSenderKeyError(
                f"sender {message.sender!r} has an undecodable public key"
            ) from exc

    # -- duplicates and conflicts ------------------------------------------

    @staticmethod
    def _same_request(stored: Any, message: NexusMessage) -> bool:
        """True when the stored request is the same logical request."""
        payload = message.payload
        assert isinstance(payload, RemoteQueryPayload)
        return (
            stored.sender_agent_id == message.sender
            and stored.recipient_agent_id == message.recipient
            and stored.query == payload.query
            and list(stored.requested_capabilities or [])
            == list(payload.requested_capabilities or [])
            and stored.created_at == message.created_at
            and stored.expires_at == message.expires_at
        )

    @staticmethod
    def _same_message(
        stored: Any, message: NexusMessage, envelope: dict[str, Any]
    ) -> bool:
        """True when the stored message is byte-identical in content."""
        return (
            stored.request_id == message.request_id
            and stored.sender_agent_id == message.sender
            and stored.recipient_agent_id == message.recipient
            and stored.message_type == message.type.value
            and stored.protocol_version == message.protocol_version
            and stored.created_at == message.created_at
            and stored.expires_at == message.expires_at
            and stored.signature == message.signature
            and (stored.envelope or {}) == envelope
        )

    async def _duplicate_outcome(
        self, session: AsyncSession, message: NexusMessage
    ) -> InboundOutcome:
        """Build the idempotent outcome from already-stored rows."""
        row = await RemoteRequestRepository(session).get(message.request_id)
        stored_message = await MessageRepository(session).get(
            message.message_id
        )
        if row is None:
            # Unreachable through the atomic insert path (request and
            # message commit together); kept as a safe fallback.
            record = RequestRecord(
                request_id=message.request_id,
                sender_agent_id=message.sender,
                recipient_agent_id=message.recipient,
                query="",
                requested_capabilities=(),
                status="",
                created_at=message.created_at,
                expires_at=message.expires_at,
            )
        else:
            record = RequestRecord.from_row(
                row,
                message_id=(
                    stored_message.message_id if stored_message else None
                ),
            )
        return InboundOutcome(
            status=DUPLICATE,
            request_id=message.request_id,
            message_id=message.message_id,
            record=record,
        )

    async def _classify_conflict(
        self, message: NexusMessage
    ) -> InboundOutcome:
        """Classify a uniqueness conflict in a fresh read transaction.

        A concurrent registration won the race: return DUPLICATE when
        the stored rows match, else reject without overwriting. The
        conflicting attempt is audited; the stored rows are untouched.
        """
        wire = message_to_dict(message)
        envelope = {
            key: value for key, value in wire.items() if key != "signature"
        }
        async with session_scope(self._sessions) as session:
            row = await RemoteRequestRepository(session).get(
                message.request_id
            )
            stored_message = await MessageRepository(session).get(
                message.message_id
            )
            exact = (
                row is not None
                and stored_message is not None
                and stored_message.request_id == message.request_id
                and self._same_request(row, message)
                and self._same_message(stored_message, message, envelope)
            ) or (
                row is not None
                and stored_message is None
                and self._same_request(row, message)
            ) or (
                row is None
                and stored_message is not None
                and self._same_message(stored_message, message, envelope)
            )
            if exact:
                return await self._duplicate_outcome(session, message)
            # Rejected without overwriting and without side effects: like
            # every other rejection, a conflict leaves no new rows, so a
            # malicious retry cannot plant audit or request records.
            raise InboundConflictingDuplicate(
                f"message {message.message_id!r} conflicts with stored "
                f"request {message.request_id!r}"
            )
