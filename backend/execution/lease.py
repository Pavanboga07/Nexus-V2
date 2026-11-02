"""Execution lease policy: one owned, expiring claim per request.

The lease is intentionally separate from outbox leases. It answers a
different question: "is an execution still alive?" It does not make
side-effecting work idempotent, and it never auto-retries. An expired
lease only makes stranded work visible and recoverable by an explicit
operator decision.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from backend.requests.states import PROCESSING

#: Static failure code recorded when an execution lease expires.
CODE_LEASE_EXPIRED = "EXECUTION_LEASE_EXPIRED"
#: Static failure code recorded when an operator resolves an
#: unrecoverable execution as failed.
CODE_OPERATOR_RESOLVED = "OPERATOR_RESOLVED"


@dataclass(frozen=True)
class LeaseConfig:
    """Lease duration for one execution claim."""

    ttl_s: int = 900
    heartbeat_s: int = 30

    def __post_init__(self) -> None:
        if self.ttl_s < 1:
            raise ValueError("lease ttl_s must be positive")
        if self.heartbeat_s < 1:
            raise ValueError("lease heartbeat_s must be positive")
        if self.heartbeat_s >= self.ttl_s:
            raise ValueError("lease heartbeat_s must be shorter than ttl_s")


@dataclass(frozen=True)
class ExecutionLease:
    """Values written when an approved request is claimed."""

    owner: str
    token: str
    expires_at: datetime
    heartbeat_at: datetime


def new_lease(
    owner: str, now: datetime, config: LeaseConfig | None = None
) -> ExecutionLease:
    """Create a fresh lease with an unguessable fencing token."""
    if not isinstance(owner, str) or not owner:
        raise ValueError("lease owner must be a non-empty string")
    settings = config or LeaseConfig()
    return ExecutionLease(
        owner=owner,
        token=secrets.token_hex(16),
        expires_at=now + timedelta(seconds=settings.ttl_s),
        heartbeat_at=now,
    )


def lease_expired(row: Any, now: datetime) -> bool:
    """True iff ``row`` has a lease whose expiry has been reached."""
    expires_at = getattr(row, "execution_lease_expires_at", None)
    return expires_at is not None and expires_at <= now


def can_recover(row: Any, now: datetime) -> bool:
    """True iff a PROCESSING row's lease is expired and recoverable."""
    return (
        getattr(row, "status", None) == PROCESSING
        and lease_expired(row, now)
    )
