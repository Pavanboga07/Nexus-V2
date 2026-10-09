"""Peer-relation data access (logical peering, not transport state)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AgentPeer
from . import _flush

_UPDATABLE = {"status", "revoked_at"}


class PeerRepository:
    """Create, load, list, update, and revoke peer relations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        local_agent_id: str,
        peer_agent_id: str,
        status: str = "pending",
    ) -> AgentPeer:
        """Insert one directed peering row."""
        peer = AgentPeer(
            local_agent_id=local_agent_id,
            peer_agent_id=peer_agent_id,
            status=status,
        )
        self._session.add(peer)
        await _flush(self._session)
        return peer

    async def get(
        self, local_agent_id: str, peer_agent_id: str
    ) -> AgentPeer | None:
        """Load one directed relation, or None."""
        result = await self._session.execute(
            select(AgentPeer).where(
                AgentPeer.local_agent_id == local_agent_id,
                AgentPeer.peer_agent_id == peer_agent_id,
            )
        )
        return result.scalars().first()

    async def list_for_local(
        self, local_agent_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[AgentPeer]:
        """All peerings of one local agent, oldest first."""
        result = await self._session.execute(
            select(AgentPeer)
            .where(AgentPeer.local_agent_id == local_agent_id)
            .order_by(AgentPeer.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def update(
        self, local_agent_id: str, peer_agent_id: str, **fields: Any
    ) -> AgentPeer | None:
        """Update stored columns (transition legality is caller policy)."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"unknown peer fields: {sorted(unknown)}")
        peer = await self.get(local_agent_id, peer_agent_id)
        if peer is None:
            return None
        for name, value in fields.items():
            setattr(peer, name, value)
        await _flush(self._session)
        return peer

    async def revoke(
        self,
        local_agent_id: str,
        peer_agent_id: str,
        revoked_at: datetime,
    ) -> AgentPeer | None:
        """Mark a relation revoked at ``revoked_at``."""
        return await self.update(
            local_agent_id, peer_agent_id,
            status="revoked", revoked_at=revoked_at,
        )

    async def delete(self, local_agent_id: str, peer_agent_id: str) -> bool:
        """Remove one directed relation; True when a row existed."""
        peer = await self.get(local_agent_id, peer_agent_id)
        if peer is None:
            return False
        await self._session.delete(peer)
        await _flush(self._session)
        return True
