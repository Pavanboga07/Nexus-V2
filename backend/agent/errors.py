"""Agent runtime error model (safe messages, chained causes)."""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """Base class for local agent runtime failures."""


class ModelConfigurationError(AgentRuntimeError):
    """Model identifier missing, malformed, or provider unavailable."""


class InvalidConfigurationError(AgentRuntimeError):
    """Non-secret operational settings are out of range."""


class InvalidInputError(AgentRuntimeError):
    """Caller input is unusable (empty, oversized, wrong roles)."""


class ConversationNotFoundError(AgentRuntimeError):
    """No conversation matches the id."""


class ConversationMismatchError(AgentRuntimeError):
    """The conversation belongs to a different agent."""


class AgentNotAvailableError(AgentRuntimeError):
    """The agent is inactive or a known remote peer, not a local owner."""


class ModelInvocationError(AgentRuntimeError):
    """The model call failed or timed out (original chained)."""


class InvalidModelOutputError(AgentRuntimeError):
    """The model returned empty, non-text, or tool-call output."""
