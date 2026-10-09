"""Nexus local agent runtime (Component 7): LangGraph + persistence.

No tools, no remote execution, no Gateway, no protocol transport —
local conversation over the existing PostgreSQL tables only.
"""

from .config import DEFAULT_SYSTEM_INSTRUCTION, AgentRuntimeConfig
from .conversation_service import ConversationService, ConversationTurn
from .errors import (
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
from .graph import build_graph, extract_text, generate_response
from .model_factory import build_chat_model, parse_model_ref
from .runtime import AgentRuntime, AssistantResponse
from .state import AgentState

__all__ = [
    "DEFAULT_SYSTEM_INSTRUCTION",
    "AgentRuntimeConfig",
    "ConversationService",
    "ConversationTurn",
    "AgentRuntimeError",
    "ModelConfigurationError",
    "InvalidConfigurationError",
    "InvalidInputError",
    "ConversationNotFoundError",
    "ConversationMismatchError",
    "AgentNotAvailableError",
    "ModelInvocationError",
    "InvalidModelOutputError",
    "build_graph",
    "extract_text",
    "generate_response",
    "build_chat_model",
    "parse_model_ref",
    "AgentRuntime",
    "AssistantResponse",
    "AgentState",
]
