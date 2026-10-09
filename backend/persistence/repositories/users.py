"""User data access (owner identities only)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from . import _flush


class UserRepository:
    """Create and load local human owners."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self) -> User:
        """Insert one user row; ids are always server-generated UUIDs."""
        user = User()
        self._session.add(user)
        await _flush(self._session)
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        """Load one user by id, or None."""
        return await self._session.get(User, user_id)

    async def delete(self, user_id: uuid.UUID) -> bool:
        """Delete one user; True when a row existed."""
        user = await self.get(user_id)
        if user is None:
            return False
        await self._session.delete(user)
        await _flush(self._session)
        return True
