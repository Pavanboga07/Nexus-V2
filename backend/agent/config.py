"""Agent runtime configuration (explicit, provider-agnostic).

The model identifier uses ``provider:model-id`` form (for example
``openai:gpt-4o``) and must be set for real execution — there is no
placeholder fallback. Secrets (API keys) never live here; the provider
libraries read them from their own environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .errors import InvalidConfigurationError

DEFAULT_SYSTEM_INSTRUCTION = (
    "You are Nexus, a helpful AI assistant. Answer the user's "
    "latest message directly, using the conversation history for context."
)


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Operational settings for one runtime instance."""

    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    request_timeout_s: float = 60.0
    max_input_chars: int = 65536
    max_history_messages: int = 20
    max_tool_result_chars: int = 8000
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION
    # Explicit opt-in for the deterministic development model
    # (``NEXUS_LLM_MODEL='deterministic:<label>'``). Never enabled
    # implicitly: production deployments without credentials must fail
    # loudly rather than silently use a fake model.
    allow_deterministic: bool = False

    def __post_init__(self) -> None:
        """Reject out-of-range operational settings immediately."""
        if self.request_timeout_s <= 0:
            raise InvalidConfigurationError(
                "request_timeout_s must be positive"
            )
        if self.max_input_chars <= 0:
            raise InvalidConfigurationError(
                "max_input_chars must be positive"
            )
        if self.max_history_messages <= 0:
            raise InvalidConfigurationError(
                "max_history_messages must be positive"
            )
        if self.max_tool_result_chars <= 0:
            raise InvalidConfigurationError(
                "max_tool_result_chars must be positive"
            )
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise InvalidConfigurationError(
                "max_output_tokens must be positive"
            )
        if self.temperature is not None and not 0.0 <= self.temperature <= 2.0:
            raise InvalidConfigurationError(
                "temperature must be within 0.0 and 2.0"
            )
        if not self.system_instruction or not self.system_instruction.strip():
            raise InvalidConfigurationError(
                "system_instruction must be non-empty"
            )

    @classmethod
    def from_env(
        cls, prefix: str = "NEXUS_LLM_", environ: Any = None
    ) -> AgentRuntimeConfig:
        """Build config from environment (composition-root entry point)."""
        env = environ if environ is not None else os.environ

        def optional_float(name: str) -> float | None:
            raw = env.get(prefix + name)
            if raw is None or raw == "":
                return None
            try:
                return float(raw)
            except ValueError as exc:
                raise InvalidConfigurationError(
                    f"{prefix + name} must be a number"
                ) from exc

        def optional_int(name: str) -> int | None:
            raw = env.get(prefix + name)
            if raw is None or raw == "":
                return None
            try:
                return int(raw)
            except ValueError as exc:
                raise InvalidConfigurationError(
                    f"{prefix + name} must be an integer"
                ) from exc

        model = env.get(prefix + "MODEL") or None
        timeout = optional_float("TIMEOUT_S")
        max_input = optional_int("MAX_INPUT_CHARS")
        max_history = optional_int("MAX_HISTORY")
        allow_raw = (env.get(prefix + "ALLOW_DETERMINISTIC") or "").lower()
        return cls(
            model=model,
            temperature=optional_float("TEMPERATURE"),
            max_output_tokens=optional_int("MAX_TOKENS"),
            **({"request_timeout_s": timeout} if timeout is not None else {}),
            **({"max_input_chars": max_input} if max_input is not None else {}),
            **({"max_history_messages": max_history}
               if max_history is not None else {}),
            allow_deterministic=allow_raw in ("1", "true", "yes", "on"),
            system_instruction=(
                env.get(prefix + "SYSTEM_INSTRUCTION")
                or DEFAULT_SYSTEM_INSTRUCTION
            ),
        )
