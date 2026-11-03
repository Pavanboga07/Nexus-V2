"""Deterministic development model (explicit opt-in, never production).

``DeterministicModel`` implements the normal LangChain chat-model
interface and nothing else: prompts flow through ``_generate`` exactly
like a real provider, but the answer is a fixed, clearly labeled
synthesis of the last user message. It exists so the full A2A path —
request, approval, execution, signing, transport, reception,
persistence — can be demonstrated and tested without paid provider
credentials.

It is only constructed when ``AgentRuntimeConfig.allow_deterministic``
is true (``NEXUS_LLM_ALLOW_DETERMINISTIC=1`` alongside
``NEXUS_LLM_MODEL='deterministic:<label>'``). Real provider behavior
is untouched, and missing production credentials still fail loudly
instead of silently falling back to this model.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class DeterministicModel(BaseChatModel):
    """Fixed, labeled answers through the standard model interface."""

    label: str = "demo"

    @property
    def _llm_type(self) -> str:
        return "deterministic"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = ""
        for message in reversed(list(messages or [])):
            content = getattr(message, "content", "")
            if isinstance(content, str) and content.strip():
                last = " ".join(content.split())[:160]
                break
        if last:
            text = f"[deterministic:{self.label}] {last}"
        else:
            text = f"[deterministic:{self.label}] (no input)"
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=text))]
        )

    async def _astream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Word-by-word stream for the development model.

        Real providers stream their own chunks; this keeps the local
        demo honest about the transport without pretending to be one.
        """
        result = self._generate(messages, stop=stop, run_manager=run_manager)
        text = result.generations[0].message.content
        for part in re.findall(r"\S+\s*", str(text)):
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content=part)
            )
            if run_manager is not None:
                await run_manager.on_llm_new_token(part, chunk=chunk)
            yield chunk
            await asyncio.sleep(0.015)
