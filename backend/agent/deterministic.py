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

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


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
