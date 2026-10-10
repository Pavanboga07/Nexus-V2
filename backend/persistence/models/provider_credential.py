"""Owner-scoped AI provider credential (encrypted key material only).

The provider API key is stored exclusively as Fernet-encrypted bytes
(see ``backend.provider_config``). No plaintext key column exists by
design; the master key lives outside the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class ProviderCredential(Base):
    """One provider configuration per owner (single-owner deployment)."""

    __tablename__ = "provider_credentials"

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    key_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False
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
