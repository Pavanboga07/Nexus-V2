"""Pairing invitations and links (trust bootstrap, not transport).

An invitation is created by User B and travels out-of-band to User A.
Only the SHA-256 of the invitation secret is stored — the secret
itself is shown once at creation and never persisted. Links track each
side's pairing state machine; peer rows remain the authorization
source for sending remote requests.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from . import Base

#: Link states owned by the pairing service (transitions there, not here).
PAIR_LINK_STATUSES = ("pending", "completed", "rejected", "expired")


class PairInvitation(Base):
    """One issued invitation (B side): secret hash, expiry, use state."""

    __tablename__ = "pair_invitations"

    invitation_id: Mapped[str] = mapped_column(
        String(128), primary_key=True
    )
    issuer_agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agents.agent_id"), nullable=False,
        index=True,
    )
    secret_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PairLink(Base):
    """One side's view of a pairing handshake (A and B each keep one)."""

    __tablename__ = "pair_links"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'completed', 'rejected', 'expired')",
            name="pair_link_status_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    local_agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agents.agent_id"), nullable=False,
        index=True,
    )
    remote_agent_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    remote_public_key: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    invitation_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
