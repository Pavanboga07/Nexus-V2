"""Request Service: durable remote-request lifecycle, no network, no agents.

Write path (one transaction: request + signed message + outbox, or
nothing)::

    validate input → load sender/recipient/peer → build + sign message
    → persist request (QUEUED) → persist message → persist outbox → commit

Read/transition paths mutate state only through the ``states`` map,
under row locks, so concurrent approvers/rejecters resolve to exactly
one winner. Incoming responses are correlated by ``request_id``,
cryptographically verified against the recipient's stored public key,
and completed atomically — terminal states never resurrect.
"""

from __future__ import annotations

import base64
import binascii
import functools
import uuid
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)
from sqlalchemy.exc import OperationalError, TimeoutError as SQLTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.errors import IdentityError
from backend.identity.signing import sign_message
from backend.identity.verification import verify_message
from backend.persistence.database import session_scope
from backend.persistence.errors import ConflictError
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.approvals import ApprovalRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.protocol.serialization import (
    ProtocolSerializationError,
    message_from_dict,
    message_to_dict,
)
from backend.protocol.types import (
    MessageType,
    is_valid_capability,
    is_valid_message_id,
    is_valid_request_id,
)
from backend.protocol.validation import ProtocolValidationError

from .errors import (
    ApprovalError,
    InactiveAgentError,
    InvalidStateTransitionError,
    PeerError,
    RequestConflictError,
    RequestServiceError,
    RequestValidationError,
    ResponseCorrelationError,
    RetryableInfrastructureError,
    UnknownAgentError,
    UnknownRequestError,
)
from .message_builder import build_remote_query_message
from .models import RequestRecord
from .ports import IdentityProvider
from .states import (
    APPROVED,
    CANCELLED,
    COMPLETED,
    DELIVERED,
    EXPIRED,
    FAILED,
    PROCESSING,
    QUEUED,
    REJECTED,
    SENT,
    WAITING_APPROVAL,
    is_terminal,
    require_transition,
)

MAX_QUERY_LENGTH = 65536
MAX_CAPABILITIES = 64


def _translate_infrastructure_errors(fn):
    """Classify transient infrastructure failures for future workers.

    Only connectivity-level failures (lost/refused connections, pool
    timeouts, socket errors) become :class:`RetryableInfrastructureError`,
    chained to the original. Domain failures, protocol/identity errors,
    and programming errors pass through untouched, so conflicts and
    illegal transitions can never look retryable. The public message is
    static: no URLs, credentials, or internals.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (OperationalError, SQLTimeoutError, OSError) as exc:
            raise RetryableInfrastructureError(
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
        raise RequestValidationError(f"{name} must be a timezone-aware datetime")
    return value


class RequestService:
    """Application service for the remote-request lifecycle."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        identities: IdentityProvider,
    ) -> None:
        self._sessions = sessions
        self._identities = identities

    # -- creation --------------------------------------------------------

    @_translate_infrastructure_errors
    async def create_remote_request(
        self,
        sender_agent_id: str,
        recipient_agent_id: str,
        query: str,
        requested_capabilities: list[Any],
        expires_at: datetime,
        conversation_id: uuid.UUID | None = None,
        request_id: str | None = None,
        message_id: str | None = None,
        now: datetime | None = None,
    ) -> RequestRecord:
        """Create, sign, and durably queue one remote request.

        ``request_id``/``message_id`` are optional caller-supplied
        explicit identifiers (otherwise both are generated); they are
        NOT idempotency keys. Reusing an already-stored ``request_id``
        — identically or with different content — raises a conflict and
        creates or overwrites nothing, and likewise for ``message_id``:
        each physical message needs its own unique id. True
        client-level idempotency (return-prior-result on retry) is
        deferred to a future API layer. The returned record is QUEUED;
        nothing touches the network.
        """
        current = _require_aware(now, "now") if now is not None else _utcnow()
        if not isinstance(query, str) or not query.strip():
            raise RequestValidationError("query must be a non-empty string")
        if len(query) > MAX_QUERY_LENGTH:
            raise RequestValidationError(
                f"query exceeds {MAX_QUERY_LENGTH} characters"
            )
        if not isinstance(requested_capabilities, list):
            raise RequestValidationError("requested_capabilities must be a list")
        if len(requested_capabilities) > MAX_CAPABILITIES:
            raise RequestValidationError(
                f"at most {MAX_CAPABILITIES} capabilities may be requested"
            )
        for capability in requested_capabilities:
            if not is_valid_capability(capability):
                raise RequestValidationError(
                    f"invalid requested capability: {capability!r}"
                )
        _require_aware(expires_at, "expires_at")
        if expires_at <= current:
            raise RequestValidationError("expires_at must be in the future")
        request_id = request_id or f"req_{uuid.uuid4().hex}"
        message_id = message_id or f"msg_{uuid.uuid4().hex}"
        if not is_valid_request_id(request_id):
            raise RequestValidationError(f"invalid request_id: {request_id!r}")
        if not is_valid_message_id(message_id):
            raise RequestValidationError(f"invalid message_id: {message_id!r}")
        if conversation_id is not None and not isinstance(
            conversation_id, uuid.UUID
        ):
            raise RequestValidationError("conversation_id must be a UUID")

        async with session_scope(self._sessions) as session:
            agents = AgentRepository(session)
            sender = await agents.get(sender_agent_id)
            if sender is None:
                raise UnknownAgentError(f"unknown sender {sender_agent_id!r}")
            if not sender.active:
                raise InactiveAgentError(
                    f"sender {sender_agent_id!r} is deactivated"
                )
            recipient = await agents.get(recipient_agent_id)
            if recipient is None:
                raise UnknownAgentError(
                    f"unknown recipient {recipient_agent_id!r}"
                )
            peer = await PeerRepository(session).get(
                sender_agent_id, recipient_agent_id
            )
            if peer is None or peer.status != "connected":
                raise PeerError(
                    f"no connected peer relationship {sender_agent_id!r} "
                    f"-> {recipient_agent_id!r}"
                )
            unsigned = build_remote_query_message(
                sender_agent_id=sender_agent_id,
                recipient_agent_id=recipient_agent_id,
                request_id=request_id,
                message_id=message_id,
                query=query,
                requested_capabilities=list(requested_capabilities),
                created_at=current,
                expires_at=expires_at,
            )
            try:
                private_key = self._identities.private_key_for(sender_agent_id)
            except KeyError as exc:
                raise RequestServiceError(
                    f"no signing identity for {sender_agent_id!r}"
                ) from exc
            signed = sign_message(unsigned, private_key)
            wire = message_to_dict(signed)
            try:
                row = await RemoteRequestRepository(session).create(
                    request_id=request_id,
                    sender_agent_id=sender_agent_id,
                    recipient_agent_id=recipient_agent_id,
                    query=query,
                    requested_capabilities=list(requested_capabilities),
                    status=QUEUED,
                    created_at=current,
                    expires_at=expires_at,
                    conversation_id=conversation_id,
                )
                await MessageRepository(session).create(
                    message_id=message_id,
                    request_id=request_id,
                    sender_agent_id=signed.sender,
                    recipient_agent_id=signed.recipient,
                    message_type=signed.type.value,
                    protocol_version=signed.protocol_version,
                    created_at=signed.created_at,
                    expires_at=signed.expires_at,
                    signature=signed.signature,
                    envelope={
                        key: value
                        for key, value in wire.items()
                        if key != "signature"
                    },
                )
                await OutboxRepository(session).create(
                    message_id, available_at=current
                )
            except ConflictError as exc:
                raise RequestConflictError(
                    f"request {request_id!r} already exists"
                ) from exc
            return RequestRecord.from_row(row, message_id=message_id)

    # -- reads -------------------------------------------------------------

    @_translate_infrastructure_errors
    async def get_request(self, request_id: str) -> RequestRecord | None:
        """Load one request view, or None (read-only, no transaction)."""
        async with self._sessions() as session:
            row = await RemoteRequestRepository(session).get(request_id)
            if row is None:
                return None
            first = await MessageRepository(session).get_by_request_id(
                request_id
            )
            return RequestRecord.from_row(
                row, message_id=first.message_id if first else None
            )

    # -- transitions -------------------------------------------------------

    async def _transition(
        self, request_id: str, target: str, **fields: Any
    ) -> RequestRecord:
        """Apply one legal, row-locked status move."""
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            row = await repo.get_for_update(request_id)
            if row is None:
                raise UnknownRequestError(f"unknown request {request_id!r}")
            require_transition(row.status, target)
            updated = await repo.update(request_id, status=target, **fields)
            assert updated is not None
            return RequestRecord.from_row(updated)

    @_translate_infrastructure_errors
    async def mark_sent(self, request_id: str) -> RequestRecord:
        """Record Gateway handoff (dispatcher-owned; no network here)."""
        return await self._transition(request_id, SENT)

    @_translate_infrastructure_errors
    async def mark_dispatched(
        self,
        request_id: str,
        message_id: str,
        *,
        claim_token: str,
        sent_at: datetime | None = None,
        now: datetime | None = None,
    ) -> RequestRecord:
        """Atomically record a confirmed Gateway handoff (dispatcher path).

        In ONE transaction: the request moves QUEUED → SENT (legal
        transitions only) and the outbox row moves to SENT, conditional
        on it still being PROCESSING under ``claim_token``. A stale
        worker holding an expired or superseded claim fails here instead
        of overwriting a newer outcome. This never marks DELIVERED:
        Gateway handoff is transport acceptance, not application receipt.
        """
        current = _require_aware(now, "now") if now is not None else _utcnow()
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            outbox = OutboxRepository(session)
            row = await repo.get_for_update(request_id)
            if row is None:
                raise UnknownRequestError(f"unknown request {request_id!r}")
            require_transition(row.status, SENT)
            sent = await outbox.mark_sent_if_claimed(
                message_id, claim_token, sent_at or current
            )
            if sent is None:
                raise RequestServiceError(
                    f"outbox claim for {message_id!r} is no longer owned "
                    "by this dispatcher"
                )
            updated = await repo.update(request_id, status=SENT)
            assert updated is not None
            return RequestRecord.from_row(updated, message_id=message_id)

    @_translate_infrastructure_errors
    async def mark_delivered(self, request_id: str) -> RequestRecord:
        """Record application-level receipt by the receiving Nexus."""
        return await self._transition(request_id, DELIVERED)

    @_translate_infrastructure_errors
    async def mark_waiting_approval(self, request_id: str) -> RequestRecord:
        """Record that the receiving side awaits its owner's decision."""
        return await self._transition(request_id, WAITING_APPROVAL)

    @_translate_infrastructure_errors
    async def mark_processing(self, request_id: str) -> RequestRecord:
        """Record that the remote runtime started (no execution here)."""
        return await self._transition(request_id, PROCESSING)

    @_translate_infrastructure_errors
    async def approve(
        self,
        request_id: str,
        approving_agent_id: str,
        reason: str | None = None,
    ) -> RequestRecord:
        """Record recipient approval + decision row, atomically."""
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            row = await repo.get_for_update(request_id)
            if row is None:
                raise UnknownRequestError(f"unknown request {request_id!r}")
            require_transition(row.status, APPROVED)
            if row.recipient_agent_id != approving_agent_id:
                raise ApprovalError(
                    f"only recipient {row.recipient_agent_id!r} may approve"
                )
            try:
                await ApprovalRepository(session).create(
                    request_id, approving_agent_id, "approved", reason
                )
            except ConflictError as exc:
                raise ApprovalError(
                    f"approval already recorded for {request_id!r}"
                ) from exc
            updated = await repo.update(request_id, status=APPROVED)
            assert updated is not None
            return RequestRecord.from_row(updated)

    @_translate_infrastructure_errors
    async def reject(
        self,
        request_id: str,
        approving_agent_id: str,
        reason: str | None = None,
    ) -> RequestRecord:
        """Record recipient rejection + decision row, atomically."""
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            row = await repo.get_for_update(request_id)
            if row is None:
                raise UnknownRequestError(f"unknown request {request_id!r}")
            require_transition(row.status, REJECTED)
            if row.recipient_agent_id != approving_agent_id:
                raise ApprovalError(
                    f"only recipient {row.recipient_agent_id!r} may reject"
                )
            try:
                await ApprovalRepository(session).create(
                    request_id, approving_agent_id, "rejected", reason
                )
            except ConflictError as exc:
                raise ApprovalError(
                    f"approval already recorded for {request_id!r}"
                ) from exc
            updated = await repo.update(request_id, status=REJECTED)
            assert updated is not None
            return RequestRecord.from_row(updated)

    @_translate_infrastructure_errors
    async def complete(
        self,
        request_id: str,
        response_message_id: str,
        completed_at: datetime | None = None,
        now: datetime | None = None,
    ) -> RequestRecord:
        """Complete against an already-persisted response message."""
        current = _require_aware(now, "now") if now is not None else _utcnow()
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            row = await repo.get_for_update(request_id)
            if row is None:
                raise UnknownRequestError(f"unknown request {request_id!r}")
            require_transition(row.status, COMPLETED)
            response = await MessageRepository(session).get(response_message_id)
            if response is None or response.request_id != request_id:
                raise ResponseCorrelationError(
                    f"response {response_message_id!r} does not belong to "
                    f"request {request_id!r}"
                )
            updated = await repo.update(
                request_id,
                status=COMPLETED,
                response_message_id=response_message_id,
                completed_at=completed_at or current,
            )
            assert updated is not None
            return RequestRecord.from_row(updated)

    @_translate_infrastructure_errors
    async def fail(
        self,
        request_id: str,
        failure_code: str,
        failure_message: str,
    ) -> RequestRecord:
        """Record an explicit failure (code required, never silent)."""
        if not isinstance(failure_code, str) or not failure_code:
            raise RequestValidationError("failure_code must be non-empty")
        if not isinstance(failure_message, str):
            raise RequestValidationError("failure_message must be a string")
        return await self._transition(
            request_id, FAILED,
            failure_code=failure_code, failure_message=failure_message,
        )

    @_translate_infrastructure_errors
    async def expire(
        self, request_id: str, now: datetime | None = None
    ) -> RequestRecord:
        """Expire against the request's own ``expires_at`` (clock-checked)."""
        current = _require_aware(now, "now") if now is not None else _utcnow()
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            row = await repo.get_for_update(request_id)
            if row is None:
                raise UnknownRequestError(f"unknown request {request_id!r}")
            if is_terminal(row.status):
                raise InvalidStateTransitionError(
                    f"illegal transition {row.status!r} -> {EXPIRED!r}"
                )
            if current < row.expires_at:
                raise InvalidStateTransitionError(
                    f"request {request_id!r} has not expired yet"
                )
            updated = await repo.update(request_id, status=EXPIRED)
            assert updated is not None
            return RequestRecord.from_row(updated)

    @_translate_infrastructure_errors
    async def cancel(self, request_id: str) -> RequestRecord:
        """Local cancellation (no remote signalling in this component)."""
        return await self._transition(request_id, CANCELLED)

    # -- response handling ---------------------------------------------------

    @_translate_infrastructure_errors
    async def handle_remote_response(
        self,
        response_envelope: dict[str, Any],
        local_agent_id: str,
        *,
        now: datetime | None = None,
    ) -> RequestRecord:
        """Correlate, verify, persist, and complete from one response.

        Never executes, never dispatches, never notifies: it only moves
        durable state from PROCESSING (or APPROVED, tolerating status
        lag on the remote side) to COMPLETED/FAILED.
        """
        current = _require_aware(now, "now") if now is not None else _utcnow()
        try:
            message = message_from_dict(response_envelope)
        except (ProtocolSerializationError, ProtocolValidationError) as exc:
            raise ResponseCorrelationError(
                f"malformed response message: {exc}"
            ) from exc
        if message.type is not MessageType.REMOTE_RESPONSE:
            raise ResponseCorrelationError(
                f"not a remote response: {message.type!r}"
            )
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            row = await repo.get_for_update(message.request_id)
            if row is None:
                raise ResponseCorrelationError(
                    f"unknown request {message.request_id!r}"
                )
            if local_agent_id != row.sender_agent_id:
                raise ResponseCorrelationError(
                    "response is not addressed to this agent's request"
                )
            if message.recipient != row.sender_agent_id:
                raise ResponseCorrelationError(
                    f"response recipient {message.recipient!r} does not "
                    "match the requesting agent"
                )
            if message.sender != row.recipient_agent_id:
                raise ResponseCorrelationError(
                    f"response sender {message.sender!r} is not the "
                    "requested agent"
                )
            if row.status not in (APPROVED, PROCESSING):
                raise ResponseCorrelationError(
                    f"request {row.request_id!r} in {row.status!r} "
                    "cannot receive a response"
                )
            responder = await AgentRepository(session).get(
                row.recipient_agent_id
            )
            if responder is None:
                raise ResponseCorrelationError(
                    f"unknown responder {row.recipient_agent_id!r}"
                )
            try:
                public_key = Ed25519PublicKey.from_public_bytes(
                    base64.urlsafe_b64decode(
                        responder.public_key
                        + "=" * (-len(responder.public_key) % 4)
                    )
                )
            except (binascii.Error, ValueError) as exc:
                raise ResponseCorrelationError(
                    "responder public key is undecodable"
                ) from exc
            try:
                verify_message(message, public_key, now=current)
            except (IdentityError, ProtocolValidationError,
                    ProtocolSerializationError) as exc:
                raise ResponseCorrelationError(
                    f"response failed verification: {exc}"
                ) from exc
            try:
                await MessageRepository(session).create(
                    message_id=message.message_id,
                    request_id=row.request_id,
                    sender_agent_id=message.sender,
                    recipient_agent_id=message.recipient,
                    message_type=message.type.value,
                    protocol_version=message.protocol_version,
                    created_at=message.created_at,
                    expires_at=message.expires_at,
                    signature=message.signature,
                    envelope={
                        key: value
                        for key, value in message_to_dict(message).items()
                        if key != "signature"
                    },
                )
            except ConflictError as exc:
                raise ResponseCorrelationError(
                    f"duplicate response {message.message_id!r}"
                ) from exc
            payload = message.payload
            if payload.status == "ok":
                updated = await repo.update(
                    row.request_id,
                    status=COMPLETED,
                    response_message_id=message.message_id,
                    completed_at=current,
                )
            else:
                updated = await repo.update(
                    row.request_id,
                    status=FAILED,
                    response_message_id=message.message_id,
                    completed_at=current,
                    failure_code=payload.error_code or "REMOTE_ERROR",
                    failure_message=payload.error_message or payload.answer,
                )
            assert updated is not None
            return RequestRecord.from_row(updated, message.message_id)
