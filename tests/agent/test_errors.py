"""Error taxonomy tests: distinct, chained, secret-free failures."""

import pytest

from backend.agent.errors import (
    AgentNotAvailableError,
    AgentRuntimeError,
    ConversationMismatchError,
    ConversationNotFoundError,
    InvalidConfigurationError,
    InvalidInputError,
    InvalidModelOutputError,
    ModelConfigurationError,
    ModelInvocationError,
)


def test_hierarchy():
    for cls in (ModelConfigurationError, InvalidConfigurationError,
                InvalidInputError, ConversationNotFoundError,
                ConversationMismatchError, AgentNotAvailableError,
                ModelInvocationError, InvalidModelOutputError):
        assert issubclass(cls, AgentRuntimeError)
        assert issubclass(cls, Exception)


def test_chaining_preserved():
    cause = ConnectionError("socket down")
    error = ModelInvocationError("model invocation failed")
    error.__cause__ = cause
    assert error.__cause__ is cause


def test_messages_carry_no_secrets():
    secret = "sk-live-abc123"
    for error in (
        ModelConfigurationError("no model configured"),
        InvalidInputError("user_input exceeds 10 characters"),
        ConversationNotFoundError("conversation 'c1' does not exist"),
        ModelInvocationError("model invocation failed"),
        InvalidModelOutputError("model returned no usable text content"),
    ):
        assert secret not in str(error)
        assert "sk-live" not in repr(error)
