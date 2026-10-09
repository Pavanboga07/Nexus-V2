"""Remote-request data access (durable logical-operation records)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RemoteRequest
from . import _flush

_UPDATABLE = {
    "status",
    "completed_at",
    "failure_code",
    "failure_message",
    "response_message_id",
    "expires_at",
    "conversation_id",
}


class RemoteRequestRepository:
    """Create, load, list, and field-update remote requests."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        request_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        query: str,
        requested_capabilities: list[Any],
        status: str,
        created_at: datetime,
        expires_at: datetime,
        conversation_id: uuid.UUID | None = None,
    ) -> RemoteRequest:
        """Insert one request row (protocol ids used verbatim)."""
        request = RemoteRequest(
            request_id=request_id,
            sender_agent_id=sender_agent_id,
            recipient_agent_id=recipient_agent_id,
            query=query,
            requested_capabilities=requested_capabilities,
            status=status,
            created_at=created_at,
            expires_at=expires_at,
            conversation_id=conversation_id,
        )
        self._session.add(request)
        await _flush(self._session)
        return request

    async def get(self, request_id: str) -> RemoteRequest | None:
        """Load one request by protocol id, or None."""
        return await self._session.get(RemoteRequest, request_id)

    async def get_for_update(self, request_id: str) -> RemoteRequest | None:
        """Load one request with a row-level write lock.

        For future concurrent workers mutating the same request: the
        lock serializes writers instead of last-write-wins.
        """
        result = await self._session.execute(
            select(RemoteRequest)
            .where(RemoteRequest.request_id == request_id)
            .with_for_update()
        )
        return result.scalars().first()

    def _ordered(self):  # small query builder shared by list methods
        return select(RemoteRequest).order_by(RemoteRequest.created_at)

    async def list_by_sender(
        self, sender_agent_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[RemoteRequest]:
        """Requests from one sender, oldest first."""
        result = await self._session.execute(
            self._ordered()
            .where(RemoteRequest.sender_agent_id == sender_agent_id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_by_recipient(
        self, recipient_agent_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[RemoteRequest]:
        """Requests to one recipient, oldest first."""
        result = await self._session.execute(
            self._ordered()
            .where(RemoteRequest.recipient_agent_id == recipient_agent_id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_by_status(
        self, status: str, *, limit: int = 100, offset: int = 0
    ) -> list[RemoteRequest]:
        """Requests in one stored status, oldest first."""
        result = await self._session.execute(
            self._ordered()
            .where(RemoteRequest.status == status)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def update(self, request_id: str, **fields: Any) -> RemoteRequest | None:
        """Update stored columns (transition legality is caller policy)."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        request = await self.get(request_id)
        if request is None:
            return None
        for name, value in fields.items():
            setattr(request, name, value)
        await _flush(self._session)
        return request

    async def save_response_reference(
        self,
        request_id: str,
        response_message_id: str,
        completed_at: datetime,
    ) -> RemoteRequest | None:
        """Attach the answering message id plus completion time."""
        return await self.update(
            request_id,
            response_message_id=response_message_id,
            completed_at=completed_at,
        )
