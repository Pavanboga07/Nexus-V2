"""Logical peer relationship (NOT transport/presence state).

A row records that ``local_agent_id`` knows ``peer_agent_id`` with a
given status. Connection liveness lives in the Gateway's in-memory
registry, never here. Status values are stored, never interpreted: the
later service layer owns legal transitions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from . import Base

#: Statuses the repository will persist; transitions are owned elsewhere.
PEER_STATUSES = ("pending", "connected", "revoked")


class AgentPeer(Base):
    """Directed logical peering between two known agents."""

    __tablename__ = "agent_peers"
    __table_args__ = (
        UniqueConstraint("local_agent_id", "peer_agent_id"),
        CheckConstraint(
            "local_agent_id != peer_agent_id", name="no_self_peer"
        ),
        CheckConstraint(
            "status IN ('pending', 'connected', 'revoked')",
            name="peer_status_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    local_agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agents.agent_id"), nullable=False
    )
    peer_agent_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agents.agent_id"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
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
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
