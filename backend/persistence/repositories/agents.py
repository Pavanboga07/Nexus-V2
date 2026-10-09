"""Agent data access (protocol identities, never secrets)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent
from . import _flush

_UPDATABLE = {"public_key", "key_algorithm", "active", "owner_user_id"}


class AgentRepository:
    """Create, load, update, and deactivate known agents."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        agent_id: str,
        public_key: str,
        owner_user_id: uuid.UUID | None = None,
        key_algorithm: str = "Ed25519",
    ) -> Agent:
        """Insert one agent row (``agent_id`` is the Component 2 id)."""
        agent = Agent(
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            public_key=public_key,
            key_algorithm=key_algorithm,
        )
        self._session.add(agent)
        await _flush(self._session)
        return agent

    async def get(self, agent_id: str) -> Agent | None:
        """Load one agent by protocol id, or None."""
        return await self._session.get(Agent, agent_id)

    async def get_by_id(self, agent_id: str) -> Agent | None:
        """Alias of :meth:`get` (protocol id is the primary key)."""
        return await self.get(agent_id)

    async def list_by_owner(
        self, owner_user_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> list[Agent]:
        """Agents owned by one user, oldest first."""
        result = await self._session.execute(
            select(Agent)
            .where(Agent.owner_user_id == owner_user_id)
            .order_by(Agent.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def update(self, agent_id: str, **fields: Any) -> Agent | None:
        """Update stored columns (no legality checks: caller policy)."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"unknown agent fields: {sorted(unknown)}")
        agent = await self.get(agent_id)
        if agent is None:
            return None
        for name, value in fields.items():
            setattr(agent, name, value)
        await _flush(self._session)
        return agent

    async def deactivate(self, agent_id: str) -> bool:
        """Mark an agent inactive; True when the row existed."""
        agent = await self.update(agent_id, active=False)
        return agent is not None
