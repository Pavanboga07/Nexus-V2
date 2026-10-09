"""Conversation data access (durable chat history, no memory engine)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Conversation, ConversationMessage
from . import _flush


class ConversationRepository:
    """Create, load, and retitle conversation threads."""

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

    async def update(self, conversation_id: Any, title: str | None) -> Conversation | None:
        """Retitle one thread (may clear the title with None)."""
        conversation = await self.get(conversation_id)
        if conversation is None:
            return None
        conversation.title = title
        await _flush(self._session)
        return conversation

    async def list_by_agent(
        self, agent_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[Conversation]:
        """Threads of one agent, oldest first."""
        result = await self._session.execute(
            select(Conversation)
            .where(Conversation.agent_id == agent_id)
            .order_by(Conversation.created_at)
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
