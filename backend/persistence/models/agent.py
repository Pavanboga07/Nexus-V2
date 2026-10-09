"""Nexus agent row: protocol identity plus local ownership.

``agent_id`` is the Component 2 ``agent_<sha256-hex>`` value, used
verbatim as the primary key — persistence never derives its own agent
identity. ``owner_user_id`` is NULL for known peer agents that are not
locally owned (both request endpoints must satisfy their foreign keys,
and peers are not local rows' owners). No private key material is
stored here, ever.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class Agent(Base):
    """A known Nexus agent: locally owned or a recorded peer identity."""

    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    key_algorithm: Mapped[str] = mapped_column(
        String(16), nullable=False, default="Ed25519"
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
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
