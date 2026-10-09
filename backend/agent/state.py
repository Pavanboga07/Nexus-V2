"""LangGraph state for the single-response workflow.

The state carries LangChain messages for history, plain text for the
current input and the produced answer, and the fixed system
instruction. One node reads everything and writes only
``response_text``; no checkpointer, no parallel branches, no hidden
channels.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


class AgentState(TypedDict, total=False):
    """Typed graph state (all fields documented, nothing implicit)."""

    system_instruction: str
    history: list[BaseMessage]
    user_input: str
    response_text: str
    # Tool-loop scratch space only: LangGraph appends the model's
    # tool-call message and each ToolNode result here. The prompt is
    # always rebuilt from history + user input + these entries, so no
    # parallel workflow state exists.
    messages: Annotated[list[BaseMessage], add_messages]
