"""Originating-side response receiver: route verified answers home.

One ``ResponseReceiver`` serves exactly one local (originating) agent
id. :meth:`handle` takes a single ``REMOTE_RESPONSE`` message — as
delivered by the existing single-consumer :class:`InboundDispatcher`
— and applies the existing request-service response handling
(:meth:`RequestService.handle_remote_response`), which re-verifies
everything against durable state inside its own transaction: envelope
parties, dispatch evidence (``SENT`` or lag-tolerant
``APPROVED``/``PROCESSING``), the persisted outbound query, the
responder's stored public key, expiry, and signature. The response
message plus terminal state commit atomically there.

Duplicate and hostile deliveries are safe by construction: the
messages table's primary key plus the terminal-state guard arbitrate,
so a redelivered response resolves to ``DUPLICATE`` and a conflicting
or late one to ``REJECTED`` — invalid responses never change request
state and never create partial rows. Deduplication never depends on
memory: a recreated receiver against the same database classifies
identically.

This component never executes, never dispatches, never invokes models
or tools, and never interprets payload meaning beyond the protocol's
``ok``/``error`` status.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.persistence.repositories.messages import MessageRepository
from backend.protocol.models import NexusMessage
from backend.protocol.serialization import (
    ProtocolSerializationError,
    message_to_dict,
)
from backend.protocol.types import MessageType
from backend.protocol.validation import ProtocolValidationError
from backend.requests.errors import ResponseCorrelationError
from backend.requests.service import RequestService

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids an import cycle
    from backend.inbound.dispatcher import InboundDispatcher

from .errors import ResponseReceiverError
from .models import DUPLICATE, REGISTERED, REJECTED, ResponseOutcome


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResponseReceiver:
    """Correlate inbound REMOTE_RESPONSE mail with outbound requests."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        local_agent_id: str,
        identities: Any,
    ) -> None:
        """Wire one originating agent to the durable response handling.

        ``identities`` satisfies the service constructor; response
        verification uses the responder's *stored* public key, never
        local private key material.
        """
        self._sessions = sessions
        self._local_agent_id = local_agent_id
        self._service = RequestService(sessions, identities)

    @property
    def local_agent_id(self) -> str:
        """The originating agent id this receiver answers for."""
        return self._local_agent_id

    async def handle(
        self, message: Any, *, now: datetime | None = None
    ) -> ResponseOutcome:
        """Process one response message into a typed outcome.

        ``REGISTERED`` means the request now rests COMPLETED/FAILED;
        ``DUPLICATE`` means this exact response was already processed;
        ``REJECTED`` means nothing was recorded. Infrastructure
        failures propagate for the caller to retry; cancellation
        propagates untouched.
        """
        if (
            not isinstance(message, NexusMessage)
            or message.type is not MessageType.REMOTE_RESPONSE
        ):
            raise ResponseReceiverError(
                "response receiver handles REMOTE_RESPONSE messages only"
            )
        current = now if now is not None else _utcnow()
        try:
            envelope = message_to_dict(message)
        except (ProtocolSerializationError, ProtocolValidationError) as exc:
            # Structurally unemittable input (e.g. a payload that does
            # not match the message type): malformed by construction,
            # so reject without touching durable state.
            return ResponseOutcome(
                status=REJECTED,
                request_id=message.request_id,
                response_message_id=None,
                detail=type(exc).__name__,
            )
        try:
            record = await self._service.handle_remote_response(
                envelope, self._local_agent_id, now=current
            )
        except ResponseCorrelationError as exc:
            return await self._classify_rejection(message, exc)
        return ResponseOutcome(
            status=REGISTERED,
            request_id=message.request_id,
            response_message_id=record.response_message_id,
            detail=record.status,
            record=record,
        )

    async def _classify_rejection(
        self, message: NexusMessage, exc: ResponseCorrelationError
    ) -> ResponseOutcome:
        """Distinguish redelivery from genuine rejection, durably.

        A stored row for this ``message_id`` under the same
        ``request_id`` proves the response was already processed (the
        service records message and terminal state atomically); a
        reused id naming a different request, or no row at all, is a
        true rejection. No exception text or payload content leaves
        this boundary.
        """
        async with self._sessions() as session:
            stored = await MessageRepository(session).get(message.message_id)
            if (
                stored is not None
                and stored.request_id == message.request_id
            ):
                record = await self._service.get_request(message.request_id)
                return ResponseOutcome(
                    status=DUPLICATE,
                    request_id=message.request_id,
                    response_message_id=message.message_id,
                    detail="already-processed",
                    record=record,
                )
        return ResponseOutcome(
            status=REJECTED,
            request_id=message.request_id,
            response_message_id=None,
            detail=type(exc).__name__,
        )

    async def attach(self, dispatcher: "InboundDispatcher") -> None:
        """Register on a dispatcher, then process retained responses.

        Registration precedes the backlog drain, so responses arriving
        mid-attach route to the handler while only pre-registration
        backlog is drained here: nothing is processed twice. Retained
        non-response messages are re-queued in order; nothing retained
        is ever discarded by starting the receiver.

        The drain also reprocesses responses the dispatcher retained
        after transient infrastructure failures, so re-attaching
        (detach, then attach) after an outage recovers them without
        loss: handling is idempotent, redelivery resolves DUPLICATE.
        """
        dispatcher.set_response_handler(self.handle)
        await self._drain_backlog(dispatcher)

    async def detach(self, dispatcher: "InboundDispatcher") -> None:
        """Unregister from a dispatcher; retained mail stays retained."""
        dispatcher.clear_response_handler(self.handle)

    async def _drain_backlog(self, dispatcher: "InboundDispatcher") -> None:
        """Process pre-registration responses; preserve other mail."""
        backlog: list[NexusMessage] = []
        while True:
            try:
                backlog.append(dispatcher.unhandled.get_nowait())
            except asyncio.QueueEmpty:
                break
        others = [
            item
            for item in backlog
            if not (
                isinstance(item, NexusMessage)
                and item.type is MessageType.REMOTE_RESPONSE
            )
        ]
        for item in others:
            await dispatcher.unhandled.put(item)
        for item in backlog:
            if (
                isinstance(item, NexusMessage)
                and item.type is MessageType.REMOTE_RESPONSE
            ):
                await self.handle(item)
