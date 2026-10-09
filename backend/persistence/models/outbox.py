"""Transactional outbox: durable dispatch records for worker claiming.

One row per physical message handed to asynchronous dispatch. The
Request Service creates the row in the SAME transaction as the request
and message rows (all-or-nothing); the Component 6 worker claims
PENDING rows under a lease token, sends them through the Gateway, and
persists SENT/FAILED/UNCERTAIN outcomes. This module stores queue
state only — no worker, no transport, no timing policy beyond the
persisted fields.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from . import Base

#: Outbox lifecycle (worker discipline; the repository stays neutral).
#: UNCERTAIN means transmission may have occurred but the outcome is
#: unproven: never an automatic retry, never a success.
PENDING = "PENDING"
PROCESSING = "PROCESSING"
SENT = "SENT"
FAILED = "FAILED"
UNCERTAIN = "UNCERTAIN"


class OutboxMessage(Base):
    """One queued physical message awaiting Gateway dispatch."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="outbox_status_allowed",
        ),
        Index("ix_outbox_status_available", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("messages.message_id"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PENDING
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transmission_started: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
