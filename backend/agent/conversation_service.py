"""Local conversation service: turns over durable PostgreSQL threads.

Sequence per turn (never a transaction across the model call):

1. short txn: load agent + conversation + bounded recent history,
   enforcing ownership, activity, and local-ownership rules;
2. close it; prepare LangChain messages (user/assistant only);
3. invoke the runtime graph (no open transaction anywhere);
4. short txn: persist the user row and the assistant row atomically;
5. return the stored turn.

A generation failure persists nothing, so retry is safe (this
tool-free runtime has no external side effects). Concurrent turns on
one conversation serialize on a per-conversation lock inside this
process; cross-process coordination is explicitly out of scope.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.persistence.database import session_scope
from backend.persistence.models import Conversation, ConversationMessage
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.conversations import (
    ConversationMessageRepository,
    ConversationRepository,
)

from .errors import (
    AgentNotAvailableError,
    ConversationMismatchError,
    ConversationNotFoundError,
    InvalidInputError,
)
from .runtime import AgentRuntime, AssistantResponse


@dataclass(frozen=True)
class ConversationTurn:
    """One persisted turn: stored ids plus the assistant response."""

    conversation_id: Any
    user_message_id: int
    assistant_message_id: int
    response: AssistantResponse
    created_at: Any


class ConversationService:
    """Conversation threads for locally owned agents (REST-ready API)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime: AgentRuntime,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._locks: dict[Any, asyncio.Lock] = {}

    def _lock_for(self, conversation_id: Any) -> asyncio.Lock:
        """Per-conversation lock (single-process turn serialization)."""
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    async def create_conversation(
        self, agent_id: str, title: str | None = None
    ) -> Conversation:
        """Open a thread for an active locally owned agent."""
        async with session_scope(self._sessions) as session:
            await self._require_local_agent(session, agent_id)
            return await ConversationRepository(session).create(
                agent_id, title=title
            )

    async def get_conversation(
        self, agent_id: str, conversation_id: Any
    ) -> Conversation:
        """Load one thread, enforcing ownership (raises otherwise)."""
        async with self._sessions() as session:
            return await self._load_owned_conversation(
                session, agent_id, conversation_id
            )

    async def list_history(
        self, agent_id: str, conversation_id: Any, *, limit: int = 100
    ) -> list[ConversationMessage]:
        """Ordered stored history (id order = deterministic)."""
        async with self._sessions() as session:
            await self._load_owned_conversation(
                session, agent_id, conversation_id
            )
            return await ConversationMessageRepository(session).list_by_conversation(
                conversation_id, limit=limit
            )

    async def list_conversations(
        self, agent_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[Conversation]:
        """Threads of one locally owned agent, oldest first."""
        async with self._sessions() as session:
            await self._require_local_agent(session, agent_id)
            return await ConversationRepository(session).list_by_agent(
                agent_id, limit=limit, offset=offset
            )

    async def submit_turn(
        self,
        agent_id: str,
        conversation_id: Any,
        user_text: str,
    ) -> ConversationTurn:
        """Run one user→assistant turn with atomic transcript writes."""
        if not isinstance(user_text, str) or not user_text.strip():
            raise InvalidInputError("user_text must be a non-empty string")
        limit = self._runtime.config.max_input_chars
        if len(user_text) > limit:
            raise InvalidInputError(
                f"user_text exceeds {limit} characters"
            )
        async with self._lock_for(conversation_id):
            async with self._sessions() as session:
                await self._require_local_agent(session, agent_id)
                await self._load_owned_conversation(
                    session, agent_id, conversation_id
                )
                stored = await ConversationMessageRepository(
                    session
                ).list_recent(
                    conversation_id,
                    self._runtime.config.max_history_messages,
                )
            history = [self._to_message(row) for row in stored]
            response = await self._runtime.run(user_text, history)
            async with session_scope(self._sessions) as session:
                messages = ConversationMessageRepository(session)
                user_row = await messages.create(
                    conversation_id, "user", user_text
                )
                assistant_row = await messages.create(
                    conversation_id, "agent", response.text
                )
            return ConversationTurn(
                conversation_id=conversation_id,
                user_message_id=user_row.id,
                assistant_message_id=assistant_row.id,
                response=response,
                created_at=assistant_row.created_at,
            )

    async def _require_local_agent(
        self, session: AsyncSession, agent_id: str
    ) -> None:
        """Human-owned, active agents only (peers are not local users)."""
        agent = await AgentRepository(session).get(agent_id)
        if agent is None or not agent.active:
            raise AgentNotAvailableError(
                f"agent {agent_id!r} is unknown or inactive"
            )
        if agent.owner_user_id is None:
            raise AgentNotAvailableError(
                f"agent {agent_id!r} is a known remote peer, not a local agent"
            )

    async def _load_owned_conversation(
        self, session: AsyncSession, agent_id: str, conversation_id: Any
    ) -> Conversation:
        conversation = await ConversationRepository(session).get(
            conversation_id
        )
        if conversation is None:
            raise ConversationNotFoundError(
                f"conversation {conversation_id!r} does not exist"
            )
        if conversation.agent_id != agent_id:
            raise ConversationMismatchError(
                "conversation belongs to a different agent"
            )
        return conversation

    @staticmethod
    def _to_message(row: ConversationMessage) -> BaseMessage:
        """Stored row → LangChain message (user/assistant only, strict)."""
        if row.role == "user":
            return HumanMessage(content=row.content)
        if row.role == "agent":
            return AIMessage(content=row.content)
        raise InvalidInputError(
            f"unsupported stored role {row.role!r}: only user and "
            "assistant history enters model context"
        )
