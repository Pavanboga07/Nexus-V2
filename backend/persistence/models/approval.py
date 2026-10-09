"""Persistent approval decision: one row per remote request at V1.

Stores the decision only. Whether approval is required, and what the
decision causes, belongs to later components — this repository never
touches remote-request state.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class Approval(Base):
    """The recorded human decision for one remote request."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="approval_decision_allowed",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("remote_requests.request_id"),
        nullable=False,
        unique=True,
    )
    approving_agent_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
