"""Remote-request lifecycle state machine (transition policy only).

The map below is the entire policy: every service transition goes
through :func:`require_transition`, so illegal moves fail loudly at
exactly one place. Terminal states have no outgoing edges, which is
what makes resurrection impossible by construction.
"""

from __future__ import annotations

from .errors import InvalidStateTransitionError

CREATED = "CREATED"
QUEUED = "QUEUED"
SENT = "SENT"
DELIVERED = "DELIVERED"
WAITING_APPROVAL = "WAITING_APPROVAL"
APPROVED = "APPROVED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

TERMINAL = frozenset({COMPLETED, REJECTED, EXPIRED, FAILED, CANCELLED})

TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({QUEUED, FAILED, CANCELLED, EXPIRED}),
    QUEUED: frozenset({SENT, FAILED, CANCELLED, EXPIRED}),
    SENT: frozenset({DELIVERED, FAILED, CANCELLED, EXPIRED}),
    DELIVERED: frozenset({WAITING_APPROVAL, FAILED, CANCELLED, EXPIRED}),
    WAITING_APPROVAL: frozenset(
        {APPROVED, REJECTED, FAILED, CANCELLED, EXPIRED}
    ),
    APPROVED: frozenset({PROCESSING, COMPLETED, FAILED, CANCELLED, EXPIRED}),
    PROCESSING: frozenset({COMPLETED, FAILED, CANCELLED, EXPIRED}),
}


def is_terminal(status: str) -> bool:
    """True iff no further transition may leave ``status``."""
    return status in TERMINAL


def allowed_next(status: str) -> frozenset[str]:
    """Legal targets from ``status`` (empty for terminal/unknown)."""
    return TRANSITIONS.get(status, frozenset())


def require_transition(current: str, target: str) -> None:
    """Raise :class:`InvalidStateTransitionError` unless legal."""
    if target not in TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransitionError(
            f"illegal transition {current!r} -> {target!r}"
        )
