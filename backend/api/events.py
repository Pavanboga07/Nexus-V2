"""Component 13 application events: hub, tickets, envelope contract.

The :class:`EventHub` is a passive, in-process fan-out: publishers
enqueue immutable :class:`AppEvent` values with non-blocking puts;
each subscriber drains its own bounded queue. There are no background
tasks, no history, and no shared mutable state beyond one instance
held by the application state — a process restart loses everything
undelivered, and clients resynchronize through the REST API.

A full subscriber queue never blocks publishers and never drops
silently: the subscriber is marked closed, its pump closes the
WebSocket with ``1013``, and the entry is removed. Ticket issuance
and redemption live in :class:`TicketStore`: single-use,
short-lived, owner-bound, and bounded.
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

#: WebSocket close code for normal completion/cancellation paths.
CLOSE_GOING_AWAY = 1001
#: WebSocket close code when a subscriber cannot keep up.
CLOSE_OVERLOADED = 1013
#: WebSocket close code for failed ticket authentication.
CLOSE_UNAUTHORIZED = 4401

#: Event types emitted by the application (exact contract, see docs).
TURN_COMPLETED = "conversation.turn.completed"
REQUEST_CREATED = "remote.request.created"
APPROVAL_REQUIRED = "remote.request.approval_required"
REQUEST_UPDATED = "remote.request.updated"
RESPONSE_RECEIVED = "remote.response.received"
EXECUTION_UPDATED = "execution.updated"

EVENT_TYPES = frozenset({
    TURN_COMPLETED,
    REQUEST_CREATED,
    APPROVAL_REQUIRED,
    REQUEST_UPDATED,
    RESPONSE_RECEIVED,
    EXECUTION_UPDATED,
})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AppEvent:
    """One immutable application notification (ids and status only)."""

    event_id: str
    type: str
    timestamp: datetime
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Wire form: ISO-8601 UTC timestamp, plain JSON values."""
        return {
            "event_id": self.event_id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "data": dict(self.data),
        }


def make_event(event_type: str, data: dict[str, Any]) -> AppEvent:
    """Build one event with a unique id and a UTC timestamp."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type!r}")
    return AppEvent(
        event_id=f"evt_{uuid.uuid4().hex}",
        type=event_type,
        timestamp=_utcnow(),
        data=dict(data),
    )


class _Subscriber:
    """One subscription: bounded queue plus a close signal."""

    __slots__ = ("queue", "closed")

    def __init__(self, queue_size: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.closed: asyncio.Event = asyncio.Event()


class EventHub:
    """Synchronous in-process fan-out to bounded subscriber queues.

    All methods are synchronous and non-blocking, so publishers
    (request handlers, service callbacks) never stall on slow
    readers. Call only from the owning event loop.
    """

    def __init__(self, queue_size: int = 100) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._subscribers: dict[str, _Subscriber] = {}

    @property
    def subscriber_count(self) -> int:
        """Live subscriptions (for tests and operators)."""
        return len(self._subscribers)

    def subscribe(self) -> str:
        """Register a subscription; return its id."""
        subscription_id = f"sub_{uuid.uuid4().hex}"
        self._subscribers[subscription_id] = _Subscriber(self._queue_size)
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription (idempotent)."""
        self._subscribers.pop(subscription_id, None)

    def queue_of(self, subscription_id: str) -> asyncio.Queue | None:
        """The draining queue for a subscription, if still present."""
        subscriber = self._subscribers.get(subscription_id)
        return subscriber.queue if subscriber is not None else None

    def close_of(self, subscription_id: str) -> asyncio.Event | None:
        """The close signal for a subscription, if still present."""
        subscriber = self._subscribers.get(subscription_id)
        return subscriber.closed if subscriber is not None else None

    def is_closed(self, subscription_id: str) -> bool:
        """True once the subscription was closed or removed."""
        subscriber = self._subscribers.get(subscription_id)
        return subscriber is None or subscriber.closed.is_set()

    def clear(self) -> None:
        """Mark every subscription closed and drop them all.

        Used at application shutdown so connection pumps observe the
        close signal even if their handler task is torn down first.
        """
        for subscriber in self._subscribers.values():
            subscriber.closed.set()
        self._subscribers.clear()

    def publish(self, event: AppEvent) -> int:
        """Enqueue one event everywhere; return deliveries made.

        A full queue marks that subscriber closed and removes it:
        its pump observes the close signal and terminates the
        connection with ``CLOSE_OVERLOADED`` instead of losing mail
        silently or stalling the publisher.
        """
        delivered = 0
        for subscription_id, subscriber in list(self._subscribers.items()):
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscriber.closed.set()
                del self._subscribers[subscription_id]
            else:
                delivered += 1
        return delivered


@dataclass
class _Ticket:
    owner_user_id: Any
    expires_at_mono: float


class TicketStore:
    """Single-use, short-lived, owner-bound WebSocket tickets.

    Tickets are 256-bit random values redeemed by exact pop: the
    first concurrent redemption wins and every later one fails, so
    simultaneous connection attempts cannot share a ticket. Storage
    is bounded; expired entries are swept on every issue.
    """

    def __init__(
        self, ttl_seconds: float = 60.0, max_tickets: int = 1024
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_tickets <= 0:
            raise ValueError("max_tickets must be positive")
        self._ttl = ttl_seconds
        self._max = max_tickets
        self._tickets: dict[str, _Ticket] = {}

    @property
    def ticket_count(self) -> int:
        """Live tickets (for tests and operators)."""
        return len(self._tickets)

    def issue(self, owner_user_id: Any) -> tuple[str, datetime]:
        """Mint one ticket; return its value and UTC expiry."""
        now_mono = time.monotonic()
        live = {
            value: ticket for value, ticket in self._tickets.items()
            if ticket.expires_at_mono > now_mono
        }
        self._tickets = live
        while len(self._tickets) >= self._max:
            self._tickets.pop(next(iter(self._tickets)))
        ticket = secrets.token_urlsafe(32)
        expires_at = _utcnow() + timedelta(seconds=self._ttl)
        self._tickets[ticket] = _Ticket(
            owner_user_id=owner_user_id,
            expires_at_mono=now_mono + self._ttl,
        )
        return ticket, expires_at

    def redeem(self, ticket: str, owner_user_id: Any) -> bool:
        """Consume one ticket iff live and bound to ``owner_user_id``.

        Unknown, expired, reused, and foreign-owner tickets all fail
        identically (False): callers must not distinguish them.
        """
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return False
        if entry.expires_at_mono <= time.monotonic():
            return False
        return entry.owner_user_id == owner_user_id
