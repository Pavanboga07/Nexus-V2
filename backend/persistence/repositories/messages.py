"""Protocol-message data access (verified envelopes, stored verbatim)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ProtocolMessage
from . import _flush


class MessageRepository:
    """Store and reload physical Nexus messages without reinterpretation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        message_id: str,
        request_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        message_type: str,
        protocol_version: str,
        created_at: datetime,
        expires_at: datetime,
        signature: str,
        envelope: dict[str, Any],
    ) -> ProtocolMessage:
        """Insert one message row (ids and envelope stored as received)."""
        message = ProtocolMessage(
            message_id=message_id,
            request_id=request_id,
            sender_agent_id=sender_agent_id,
            recipient_agent_id=recipient_agent_id,
            message_type=message_type,
            protocol_version=protocol_version,
            created_at=created_at,
            expires_at=expires_at,
            signature=signature,
            envelope=envelope,
        )
        self._session.add(message)
        await _flush(self._session)
        return message

    async def get(self, message_id: str) -> ProtocolMessage | None:
        """Load one message by protocol id, or None."""
        return await self._session.get(ProtocolMessage, message_id)

    async def get_by_request_id(self, request_id: str) -> ProtocolMessage | None:
        """Earliest stored message for a request, or None."""
        result = await self._session.execute(
            select(ProtocolMessage)
            .where(ProtocolMessage.request_id == request_id)
            .order_by(ProtocolMessage.created_at)
            .limit(1)
        )
        return result.scalars().first()

    async def list_by_request(
        self, request_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[ProtocolMessage]:
        """All stored messages for a request, oldest first."""
        result = await self._session.execute(
            select(ProtocolMessage)
            .where(ProtocolMessage.request_id == request_id)
            .order_by(ProtocolMessage.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())
