"""Conversation data access (durable chat history, no memory engine)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Conversation, ConversationMessage
from . import _flush


class ConversationRepository:
    """Create, load, organize, and delete conversation threads."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, agent_id: str, title: str | None = None
    ) -> Conversation:
        """Insert one conversation thread."""
        conversation = Conversation(agent_id=agent_id, title=title)
        self._session.add(conversation)
        await _flush(self._session)
        return conversation

    async def get(self, conversation_id: Any) -> Conversation | None:
        """Load one thread by id, or None."""
        return await self._session.get(Conversation, conversation_id)

    async def rename(
        self, conversation_id: Any, title: str | None
    ) -> Conversation | None:
        """Retitle one thread (may clear the title with None)."""
        conversation = await self.get(conversation_id)
        if conversation is None:
            return None
        conversation.title = title
        await _flush(self._session)
        await self._session.refresh(conversation)
        return conversation

    async def update(
        self, conversation_id: Any, title: str | None
    ) -> Conversation | None:
        """Backward-compatible alias for :meth:`rename`."""
        return await self.rename(conversation_id, title)

    async def set_pinned(
        self, conversation_id: Any, pinned: bool
    ) -> Conversation | None:
        """Pin or unpin one thread."""
        conversation = await self.get(conversation_id)
        if conversation is None:
            return None
        conversation.pinned = pinned
        await _flush(self._session)
        await self._session.refresh(conversation)
        return conversation

    async def touch(self, conversation_id: Any) -> None:
        """Mark a thread active so recency ordering reflects new turns."""
        await self._session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(updated_at=func.now())
        )

    async def delete(self, conversation_id: Any) -> bool:
        """Delete a thread and its messages (no orphaned history)."""
        conversation = await self.get(conversation_id)
        if conversation is None:
            return False
        await self._session.execute(
            delete(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation_id
            )
        )
        await self._session.delete(conversation)
        await _flush(self._session)
        return True

    async def list_by_agent(
        self,
        agent_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
    ) -> list[Conversation]:
        """Threads of one agent: pinned first, then most recently active.

        ``query`` matches the title or any stored message, so a user can
        find a thread by what was said rather than only by its title.
        """
        clauses = [Conversation.agent_id == agent_id]
        needle = (query or "").strip()
        if needle:
            clauses.append(
                or_(
                    Conversation.title.icontains(needle, autoescape=True),
                    exists().where(
                        ConversationMessage.conversation_id == Conversation.id,
                        ConversationMessage.content.icontains(
                            needle, autoescape=True
                        ),
                    ),
                )
            )
        result = await self._session.execute(
            select(Conversation)
            .where(*clauses)
            .order_by(
                Conversation.pinned.desc(),
                Conversation.updated_at.desc(),
                Conversation.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())


class ConversationMessageRepository:
    """Append and replay utterances within a thread."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        conversation_id: Any,
        role: str,
        content: str,
        request_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ConversationMessage:
        """Append one utterance (optionally linked to a remote request)."""
        message = ConversationMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            request_id=request_id,
            meta=meta,
        )
        self._session.add(message)
        await _flush(self._session)
        await ConversationRepository(self._session).touch(conversation_id)
        return message

    async def list_by_conversation(
        self, conversation_id: Any, *, limit: int = 100, offset: int = 0
    ) -> list[ConversationMessage]:
        """Utterances of one thread in insertion order."""
        result = await self._session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_recent(
        self, conversation_id: Any, limit: int
    ) -> list[ConversationMessage]:
        """Most recent ``limit`` utterances, oldest first.

        Added for the agent runtime: bounding model context needs the
        tail without loading unbounded history. ``id`` ordering is the
        stable tiebreaker for equal timestamps.
        """
        result = await self._session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.id.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))
