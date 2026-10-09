"""Request Service domain records (immutable snapshots, not ORM rows)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.persistence.models import RemoteRequest


@dataclass(frozen=True)
class RequestRecord:
    """Point-in-time view of one durable remote request."""

    request_id: str
    sender_agent_id: str
    recipient_agent_id: str
    query: str
    requested_capabilities: tuple[Any, ...]
    status: str
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    response_message_id: str | None = None
    conversation_id: uuid.UUID | None = None
    message_id: str | None = None

    @classmethod
    def from_row(
        cls, row: RemoteRequest, message_id: str | None = None
    ) -> RequestRecord:
        """Snapshot a persistence row (capabilities frozen to a tuple)."""
        return cls(
            request_id=row.request_id,
            sender_agent_id=row.sender_agent_id,
            recipient_agent_id=row.recipient_agent_id,
            query=row.query,
            requested_capabilities=tuple(row.requested_capabilities or []),
            status=row.status,
            created_at=row.created_at,
            expires_at=row.expires_at,
            completed_at=row.completed_at,
            failure_code=row.failure_code,
            failure_message=row.failure_message,
            response_message_id=row.response_message_id,
            conversation_id=row.conversation_id,
            message_id=message_id,
        )
