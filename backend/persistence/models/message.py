"""One persisted physical Nexus protocol message.

The ``envelope`` JSONB column holds the exact Component 1 wire mapping
(protocol_version, message_id, request_id, sender, recipient, type,
created_at, expires_at, payload) plus nothing else; the ``signature``
lives in its own column for cheap inspection. Repositories never
re-sign, normalize, or reinterpret the envelope: what was verified on
the wire is what is stored, so cryptographic validity survives the
round trip. Sender/recipient are indexed text, not foreign keys — the
counterparty of a stored message is frequently a peer without a local
agent row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class ProtocolMessage(Base):
    """A stored Nexus protocol envelope with its detached signature."""

    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("remote_requests.request_id"),
        nullable=False,
        index=True,
    )
    sender_agent_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    recipient_agent_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    message_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    protocol_version: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    signature: Mapped[str] = mapped_column(String(256), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
