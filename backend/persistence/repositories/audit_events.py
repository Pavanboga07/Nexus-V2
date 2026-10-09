"""Audit-event data access (append-only facts, bounded metadata)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditEvent
from . import _flush


class AuditEventRepository:
    """Append audit facts and query them by correlation id."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        event_type: str,
        actor_agent_id: str | None = None,
        request_id: str | None = None,
        message_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append one audit fact."""
        event = AuditEvent(
            event_type=event_type,
            actor_agent_id=actor_agent_id,
            request_id=request_id,
            message_id=message_id,
            meta=meta,
        )
        self._session.add(event)
        await _flush(self._session)
        return event

    async def list_by_request(
        self, request_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[AuditEvent]:
        """Facts correlated to one request, oldest first."""
        result = await self._session.execute(
            select(AuditEvent)
            .where(AuditEvent.request_id == request_id)
            .order_by(AuditEvent.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_by_message(
        self, message_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[AuditEvent]:
        """Facts correlated to one message, oldest first."""
        result = await self._session.execute(
            select(AuditEvent)
            .where(AuditEvent.message_id == message_id)
            .order_by(AuditEvent.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())
