"""Single-consumer inbound dispatch: queries to Component 9, rest retained.

The GatewayClient owns exactly one ``recv()`` reader that demultiplexes
ACKs to send-waiters and DELIVERs to its ``incoming`` queue. This module
owns the single consumer of that queue: :class:`InboundDispatcher`
routes ``REMOTE_QUERY`` messages to the :class:`InboundProcessor` and
retains every other message type in its ``unhandled`` queue for the
future response receiver. Nothing is silently discarded, and no second
consumer of the same queue may exist while a dispatcher runs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from backend.protocol.models import NexusMessage
from backend.protocol.types import MessageType

from .errors import InboundError
from .models import DUPLICATE, REGISTERED, InboundOutcome
from .processor import InboundProcessor

logger = logging.getLogger("nexus.inbound")

#: A query was registered or recognised as a duplicate.
HANDLED = "HANDLED"
#: A non-query message was retained for a future consumer.
UNHANDLED = "UNHANDLED"
#: A query was rejected; the typed reason is attached.
REJECTED = "REJECTED"


class DispatcherAlreadyRunning(InboundError):
    """A second consumer of the same inbound source was attempted."""


class DispatchResult:
    """One routing decision (small immutable value object)."""

    __slots__ = ("status", "message_id", "request_id", "detail")

    def __init__(
        self,
        status: str,
        message_id: str,
        request_id: str = "",
        detail: str = "",
    ) -> None:
        self.status = status
        self.message_id = message_id
        self.request_id = request_id
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return (
            f"DispatchResult(status={self.status!r}, "
            f"message_id={self.message_id!r}, "
            f"request_id={self.request_id!r}, detail={self.detail!r})"
        )


class InboundDispatcher:
    """Route inbound messages; retain what this component must not run."""

    def __init__(
        self,
        processor: InboundProcessor,
        *,
        unhandled_queue_size: int = 100,
    ) -> None:
        """Bind a processor; create the retention queue for other types."""
        self._processor = processor
        self.unhandled: asyncio.Queue[NexusMessage] = asyncio.Queue(
            maxsize=unhandled_queue_size
        )
        self._task: asyncio.Task[None] | None = None
        # The GatewayClient this dispatcher was bound to via
        # :meth:`from_client` (None for a raw queue source, where no
        # ownership can be identified or enforced).
        self._client: Any | None = None

    @property
    def processor(self) -> InboundProcessor:
        """The query processor this dispatcher routes to."""
        return self._processor

    @property
    def running(self) -> bool:
        """True while the consumer task owns the inbound source."""
        return self._task is not None and not self._task.done()

    @classmethod
    def from_client(
        cls, client: Any, processor: InboundProcessor, **kwargs: Any
    ) -> tuple["InboundDispatcher", Callable[[], Awaitable[Any]]]:
        """Bind a dispatcher to a GatewayClient without touching it.

        Returns the dispatcher plus a zero-argument receive callable
        over ``client.receive_inbound``. The client's reader, ACK, and
        queue rules are unchanged; this only consumes what it queued.
        The dispatcher remembers the client so :meth:`start` can
        enforce exclusive ownership of its queue.
        """
        dispatcher = cls(processor, **kwargs)
        dispatcher._client = client
        return dispatcher, client.receive_inbound

    async def dispatch_one(self, message: Any) -> DispatchResult:
        """Route exactly one message (queries handled, rest retained)."""
        if not isinstance(message, NexusMessage):
            logger.warning("inbound: dropping non-message object")
            return DispatchResult(REJECTED, "", detail="not-a-message")
        if message.type is MessageType.REMOTE_QUERY:
            try:
                outcome: InboundOutcome = await self._processor.process(
                    message
                )
            except asyncio.CancelledError:
                raise
            except InboundError as exc:
                logger.warning(
                    "inbound query %s rejected: %s",
                    message.message_id,
                    type(exc).__name__,
                )
                return DispatchResult(
                    REJECTED,
                    message.message_id,
                    message.request_id,
                    type(exc).__name__,
                )
            except Exception as exc:  # noqa: BLE001 - loop stays alive
                logger.error(
                    "inbound query %s failed unexpectedly: %s",
                    message.message_id,
                    type(exc).__name__,
                )
                return DispatchResult(
                    REJECTED,
                    message.message_id,
                    message.request_id,
                    type(exc).__name__,
                )
            logger.info(
                "inbound query %s %s %s",
                message.message_id,
                message.request_id,
                outcome.status,
            )
            return DispatchResult(
                HANDLED,
                message.message_id,
                message.request_id,
                outcome.status,
            )
        # Retained, never dropped: the future response receiver (and
        # any later handler) consumes this queue. Blocking put applies
        # backpressure instead of losing application messages.
        await self.unhandled.put(message)
        logger.info(
            "inbound %s %s retained for future handling",
            message.type.value
            if isinstance(message.type, MessageType)
            else message.type,
            message.message_id,
        )
        return DispatchResult(
            UNHANDLED, message.message_id, message.request_id,
            (
                message.type.value
                if isinstance(message.type, MessageType)
                else str(message.type)
            ),
        )

    async def next_unhandled(self) -> NexusMessage:
        """Yield the next retained message (future response receiver)."""
        return await self.unhandled.get()

    async def run(
        self, receive: Callable[[], Awaitable[Any]]
    ) -> None:
        """Consume one source until cancelled (no return on success)."""
        while True:
            message = await receive()
            await self.dispatch_one(message)

    def start(
        self, receive: Callable[[], Awaitable[Any]]
    ) -> "asyncio.Task[None]":
        """Start the single consumer task (raises if one already runs).

        When bound to a client, claims exclusive ownership of its
        queue first: a second dispatcher bound to the same client
        fails here instead of silently splitting messages. The
        check-and-set is synchronous, so concurrent startups on one
        event loop cannot both succeed.
        """
        if self.running:
            raise DispatcherAlreadyRunning(
                "an inbound consumer is already running"
            )
        self._claim_queue()
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run_owned(receive))
            # A task cancelled before its first step never executes the
            # coroutine body (not even finally): the done callback is
            # the only release path for that case. It is identity
            # guarded, so restarts and replacements are never disturbed.
            self._task.add_done_callback(self._on_task_done)
        except BaseException:
            self._release_queue()
            raise
        return self._task

    def _on_task_done(self, task: "asyncio.Task[None]") -> None:
        """Release ownership when the owned task ends for any reason."""
        if self._task is task:
            self._release_queue()

    def _claim_queue(self) -> None:
        """Take client-queue ownership for this dispatcher (or raise)."""
        client = self._client
        if client is None:
            return
        owner = getattr(client, "_inbound_owner", None)
        if owner is not None and owner is not self:
            raise DispatcherAlreadyRunning(
                "this client's incoming queue is already owned by "
                "another inbound dispatcher"
            )
        try:
            client._inbound_owner = self
        except AttributeError as exc:
            raise DispatcherAlreadyRunning(
                "cannot track ownership on this inbound source"
            ) from exc

    def _release_queue(self) -> None:
        """Release client-queue ownership if held by this dispatcher."""
        client = self._client
        if client is None:
            return
        try:
            if client._inbound_owner is self:
                client._inbound_owner = None
        except AttributeError:
            pass

    async def _run_owned(
        self, receive: Callable[[], Awaitable[Any]]
    ) -> None:
        try:
            await self.run(receive)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - loop must not die silent
            logger.error("inbound consumer failed: %s", type(exc).__name__)
            raise
        finally:
            # Cancellation, crash, or shutdown always frees the queue
            # for a replacement dispatcher; ownership never strands.
            self._release_queue()

    async def stop(self) -> None:
        """Cancel the consumer task and wait for it (idempotent).

        Ownership is released whether the task was running, already
        finished, or never started, so a replacement dispatcher can
        start afterwards. A release never steals another dispatcher's
        live ownership.
        """
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._release_queue()


__all__ = [
    "HANDLED",
    "REJECTED",
    "UNHANDLED",
    "DispatchResult",
    "DispatcherAlreadyRunning",
    "InboundDispatcher",
]
