"""Security/application audit trail (facts only, never secrets).

Rows record that something happened: who, what, when, and bounded
structured context. Private keys, credentials, tokens, and full
sensitive tool results must never be written here — enforced by caller
discipline (later components), since the database cannot distinguish
secret strings from ordinary ones.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class AuditEvent(Base):
    """One auditable fact, correlatable by request or message id."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_agent_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    # ``meta`` maps the ``metadata`` column: ``metadata`` itself is a
    # reserved attribute name on declarative models.
    meta: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
