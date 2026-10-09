"""Nexus outbox worker (Component 6): dispatch, never execution.

:class:`OutboxDispatcher` claims durable outbox rows and hands them to
the Gateway through pooled authenticated clients, persisting graded
outcomes. It transports bytes; it never reasons, never runs tools.
"""

from .config import WorkerConfig
from .errors import MissingIdentityError, WorkerError
from .models import DispatchOutcome, DispatchResult, compute_backoff
from .outbox_dispatcher import OutboxDispatcher

__all__ = [
    "WorkerConfig",
    "MissingIdentityError",
    "WorkerError",
    "DispatchOutcome",
    "DispatchResult",
    "compute_backoff",
    "OutboxDispatcher",
]
