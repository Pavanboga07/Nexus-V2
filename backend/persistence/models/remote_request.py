"""Durable record of one logical remote operation.

Status is stored, never interpreted: Component 5 owns legal state
transitions. ``response_message_id`` deliberately carries no foreign
key (it would cycle messages → requests → messages); Component 5
resolves it after the response row exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class RemoteRequest(Base):
    """One logical remote operation from a sender agent to a recipient."""

    __tablename__ = "remote_requests"

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sender_agent_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agents.agent_id"),
        nullable=False,
        index=True,
    )
    recipient_agent_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agents.agent_id"),
        nullable=False,
        index=True,
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    requested_capabilities: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    # Optional conversation correlation, deliberately without a foreign
    # key: the linked thread may be created independently of the request
    # (same rationale as response_message_id and the conversation-side
    # request_id). Component 5 only preserves the value.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_remote_requests_status", "status"),
        Index("ix_remote_requests_created_at", "created_at"),
    )
