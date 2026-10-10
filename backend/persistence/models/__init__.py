"""SQLAlchemy models: Base, constraint naming, and model registry.

Importing this package registers every table on ``Base.metadata`` (the
Alembic ``target_metadata`` and the schema-match test both rely on it).
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint names (Alembic-safe)."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


from .agent import Agent  # noqa: E402
from .approval import Approval  # noqa: E402
from .audit_event import AuditEvent  # noqa: E402
from .conversation import Conversation, ConversationMessage  # noqa: E402
from .message import ProtocolMessage  # noqa: E402
from .outbox import OutboxMessage  # noqa: E402
from .pairing import PairInvitation, PairLink  # noqa: E402
from .peer import AgentPeer  # noqa: E402
from .provider_credential import ProviderCredential  # noqa: E402
from .remote_request import RemoteRequest  # noqa: E402
from .user import User  # noqa: E402

__all__ = [
    "Base",
    "User",
    "Agent",
    "AgentPeer",
    "RemoteRequest",
    "ProtocolMessage",
    "OutboxMessage",
    "Approval",
    "Conversation",
    "ConversationMessage",
    "AuditEvent",
    "ProviderCredential",
    "PairInvitation",
    "PairLink",
]
