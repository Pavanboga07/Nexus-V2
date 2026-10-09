"""Stable public runtime over the compiled graph (one graph per instance).

``AgentRuntime.run`` validates input, bounds history, invokes the
already-compiled LangGraph workflow with a timeout, and returns a
structured, secret-free :class:`AssistantResponse`. Model failures
surface as :class:`ModelInvocationError` (chained); invalid model
output surfaces as :class:`InvalidModelOutputError` unchanged. Nothing
is fabricated, retried, or executed here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from .config import AgentRuntimeConfig
from .errors import (
    InvalidInputError,
    InvalidModelOutputError,
    ModelInvocationError,
)
from .graph import build_graph
from .model_factory import build_chat_model


@dataclass(frozen=True)
class AssistantResponse:
    """The documented public result: answer text plus safe model label."""

    text: str
    model_name: str


class AgentRuntime:
    """One compiled graph plus its chat model and configuration."""

    def __init__(
        self,
        config: AgentRuntimeConfig,
        model: BaseChatModel | None = None,
        tools: Sequence[Any] | None = None,
    ) -> None:
        """Build once: model (injected or factory), tools bound, compiled.

        ``tools`` is the runtime's complete permitted set, supplied by
        the trusted caller (for example the MCP manager's allowlisted
        tools or a narrower request-specific subset later). ``None``
        preserves the exact Component 7 tool-free behavior.
        """
        self._config = config
        self._model = model if model is not None else build_chat_model(config)
        self._tools = tuple(tools) if tools else ()
        self._graph = build_graph(
            self._model,
            self._tools or None,
            max_tool_result_chars=config.max_tool_result_chars,
        )

    @property
    def config(self) -> AgentRuntimeConfig:
        """The configuration this runtime was built with."""
        return self._config

    @property
    def graph(self):
        """The compiled LangGraph workflow (reused across turns)."""
        return self._graph

    @property
    def model_name(self) -> str:
        """Configured model id, or ``"injected"`` for supplied models."""
        return self._config.model or "injected"

    @property
    def tools(self) -> tuple[Any, ...]:
        """This instance's permitted tool set (possibly empty)."""
        return self._tools

    def _bounded_history(
        self, history: Sequence[BaseMessage] | None
    ) -> list[BaseMessage]:
        """Validate roles and keep the most recent N messages in order."""
        items = list(history) if history else []
        for message in items:
            if not isinstance(message, (HumanMessage, AIMessage)):
                raise InvalidInputError(
                    "history supports only user and assistant messages, "
                    f"got {type(message).__name__}"
                )
        return items[-self._config.max_history_messages :]

    async def run(
        self,
        user_input: str,
        history: Sequence[BaseMessage] | None = None,
        *,
        system_instruction: str | None = None,
    ) -> AssistantResponse:
        """Generate one assistant reply for ``user_input``."""
        if not isinstance(user_input, str) or not user_input.strip():
            raise InvalidInputError("user_input must be a non-empty string")
        if len(user_input) > self._config.max_input_chars:
            raise InvalidInputError(
                f"user_input exceeds {self._config.max_input_chars} characters"
            )
        system = system_instruction or self._config.system_instruction
        if not isinstance(system, str) or not system.strip():
            raise InvalidInputError("system_instruction must be non-empty")
        state = {
            "system_instruction": system,
            "history": self._bounded_history(history),
            "user_input": user_input,
        }
        try:
            outcome: dict[str, Any] = await asyncio.wait_for(
                self._graph.ainvoke(state),
                timeout=self._config.request_timeout_s,
            )
        except InvalidModelOutputError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ModelInvocationError(
                "model invocation timed out"
            ) from exc
        except Exception as exc:
            raise ModelInvocationError(
                "model invocation failed"
            ) from exc
        text = outcome.get("response_text", "")
        if not isinstance(text, str) or not text.strip():
            raise InvalidModelOutputError(
                "model returned no usable text content"
            )
        return AssistantResponse(text=text, model_name=self.model_name)
