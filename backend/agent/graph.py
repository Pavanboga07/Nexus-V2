"""The LangGraph workflow: single response, optional tool loop.

Without tools the graph is exactly Component 7::

    START → generate_response → END

With an explicitly supplied tool set it becomes::

    START → generate_response → conditional
        → ToolNode → generate_response → …
        → END (when the answer is final)

The node builds [system, *history, current user message] plus any
prior tool-loop messages from state, invokes the pre-bound chat model
exactly once per visit, and either returns validated final text or
hands the model's tool calls to the router. Unknown tool names are
rejected before any execution. No environment access, no model
construction inside nodes, no handwritten orchestration loop.
"""

from __future__ import annotations

import functools
from typing import Any, Sequence
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .errors import InvalidModelOutputError, ModelConfigurationError
from .state import AgentState


def extract_text(content: Any) -> str:
    """Fold supported LangChain text content into one string.

    Accepts plain strings and content-block lists (``str`` items and
    ``{"type": "text", "text": ...}`` blocks); anything else yields no
    text. Non-text blocks are ignored here and an all-non-text result
    is rejected by the caller — never executed, never rendered.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def tool_calls_of(message: Any) -> list[dict[str, Any]]:
    """Tool calls declared by a message (dicts with names), or []."""
    calls = getattr(message, "tool_calls", None) or []
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if isinstance(call, dict) and isinstance(call.get("name"), str):
            normalized.append(call)
        else:
            name = getattr(call, "name", None)
            if isinstance(name, str):
                normalized.append({"name": name})
    return normalized


def _reject_tool_calls(message: AIMessage) -> None:
    """Refuse tool-call output: this runtime has no tools to run."""
    if tool_calls_of(message):
        raise InvalidModelOutputError(
            "model returned tool calls, but this runtime has no tools"
        )
    invalid = getattr(message, "invalid_tool_calls", None)
    if invalid:
        raise InvalidModelOutputError(
            "model returned malformed tool calls, which are unsupported"
        )


def _validated_text(result: Any) -> str:
    """Require an assistant message with usable text (no tools here)."""
    if not isinstance(result, AIMessage):
        raise InvalidModelOutputError(
            f"model returned {type(result).__name__}, not an assistant message"
        )
    _reject_tool_calls(result)
    text = extract_text(result.content).strip()
    if not text:
        raise InvalidModelOutputError(
            "model returned no usable text content"
        )
    return text


async def generate_response(
    model: Any, state: AgentState, tools: Sequence[Any] = ()
) -> dict[str, Any]:
    """Graph node: one model call; final text or tool-call handoff out."""
    prompt = [
        SystemMessage(content=state["system_instruction"]),
        *state["history"],
        HumanMessage(content=state["user_input"]),
    ]
    prior = list(state.get("messages") or [])
    if not tools:
        result = await model.ainvoke(prompt)
        return {"response_text": _validated_text(result)}
    result = await model.ainvoke(prompt + prior)
    calls = tool_calls_of(result) if isinstance(result, AIMessage) else []
    if not calls:
        text = extract_text(result.content).strip() if isinstance(
            result, AIMessage) else ""
        if not text:
            raise InvalidModelOutputError(
                "model returned no usable text content"
            )
        return {"response_text": text}
    return {"messages": [result]}


def route_after_generate(
    state: AgentState, allowed_tool_names: frozenset[str]
) -> str:
    """Conditional edge: validated tool calls go to tools, else END."""
    messages = state.get("messages") or []
    if not messages:
        return "end"
    calls = tool_calls_of(messages[-1])
    if not calls:
        return "end"
    unknown = sorted(
        {call["name"] for call in calls if call["name"] not in allowed_tool_names}
    )
    if unknown:
        raise InvalidModelOutputError(
            f"model called tools outside this runtime's set: {unknown}"
        )
    return "tools"


def _truncate_text(value: Any, limit: int) -> Any:
    """Bound one tool-result value (strings cut with a marker)."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n…[truncated {len(value) - limit} chars]"
    if isinstance(value, list):
        return [_truncate_text(item, limit) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            text = value["text"]
            if len(text) > limit:
                return {**value, "text": text[:limit] + f"\n…[truncated {len(text) - limit} chars]"}
        return value
    return value


def _bound_artifact(artifact: Any, limit: int) -> Any:
    """Drop oversized artifacts with a deterministic marker.

    Small artifacts pass through untouched. An artifact whose JSON
    size exceeds ``limit`` — or whose size cannot be measured at all
    (serialization failure) — is replaced by a small descriptor dict
    so one chatty tool cannot bloat checkpoints and carried state.
    Truncating arbitrary artifact objects would corrupt them silently;
    an explicit marker keeps the drop visible and deterministic.
    """
    import json

    try:
        size: int | None = len(json.dumps(
            artifact, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 - measurement probe, fail closed below
        size = None
    if size is None:
        return {
            "type": "artifact_dropped",
            "reason": "artifact size is unmeasurable (not serializable)",
        }
    if size > limit:
        return {
            "type": "artifact_dropped",
            "reason": f"artifact size {size} chars exceeds bound {limit}",
        }
    return artifact


class BoundedToolNode(ToolNode):
    """ToolNode with bounded result text and artifacts (context protection).

    Execution, error mapping, and message shapes stay exactly
    ToolNode's; only over-long text content is cut with a marker so one
    chatty tool cannot exhaust model context. Artifacts are bounded too:
    small ones pass through untouched, oversized or unmeasurable ones
    are replaced by a deterministic drop marker (see
    :func:`_bound_artifact`) so carried state and checkpoints stay
    bounded. Neither content truncation nor artifact bounding changes
    the public conversation result contract.
    """

    def __init__(self, tools: Sequence[Any], *, max_chars: int = 8000, **kwargs: Any) -> None:
        super().__init__(tools, **kwargs)
        self.max_chars = max_chars

    @staticmethod
    def _args_size(calls: list[dict[str, Any]]) -> float:
        import json

        try:
            return float(len(json.dumps(
                [call.get("args", {}) for call in calls], ensure_ascii=False)))
        except (TypeError, ValueError):
            return float("inf")  # unmeasurable: fail closed below

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        messages = input.get("messages", []) if isinstance(input, dict) else []
        pending = [
            call for message in messages if isinstance(message, AIMessage)
            for call in tool_calls_of(message)
        ]
        if self._args_size(pending) > self.max_chars:
            raise InvalidModelOutputError(
                "model tool-call arguments exceed the bounded size"
            )
        outcome = await super().ainvoke(input, config, **kwargs)
        messages = outcome.get("messages", []) if isinstance(outcome, dict) else []
        bounded = []
        for message in messages:
            if isinstance(message, ToolMessage):
                update: dict[str, Any] = {}
                if isinstance(message.content, (str, list)):
                    update["content"] = _truncate_text(
                        message.content, self.max_chars)
                if message.artifact is not None:
                    update["artifact"] = _bound_artifact(
                        message.artifact, self.max_chars)
                bounded.append(
                    message.model_copy(update=update) if update
                    else message)
            else:
                bounded.append(message)
        return {"messages": bounded}


def build_graph(
    model: BaseChatModel,
    tools: Sequence[Any] | None = None,
    *,
    max_tool_result_chars: int = 8000,
) -> Any:
    """Compile the workflow around ``model`` (and optional tools).

    Without tools this is exactly the Component 7 graph. With tools
    the model is bound once via its own ``bind_tools`` API (models
    without tool support fail here with a clear configuration error),
    and a bounded ToolNode executes validated calls inside the graph.
    """
    tool_list = list(tools) if tools else []
    for tool in tool_list:
        if not isinstance(getattr(tool, "name", None), str) or not tool.name:
            raise ModelConfigurationError(
                "runtime tools must be LangChain tools with names"
            )
    if not tool_list:
        graph = StateGraph(AgentState)
        graph.add_node(
            "generate_response", functools.partial(generate_response, model)
        )
        graph.add_edge(START, "generate_response")
        graph.add_edge("generate_response", END)
        return graph.compile()
    binder = getattr(model, "bind_tools", None)
    if not callable(binder):
        raise ModelConfigurationError(
            "tools are configured but the model does not support "
            "tool calling (no bind_tools API)"
        )
    bound = model.bind_tools(tool_list)
    allowed = frozenset(tool.name for tool in tool_list)
    graph = StateGraph(AgentState)
    graph.add_node(
        "generate_response",
        functools.partial(generate_response, bound, tools=tool_list),
    )
    graph.add_node(
        "tools", BoundedToolNode(tool_list, max_chars=max_tool_result_chars)
    )
    graph.add_edge(START, "generate_response")
    graph.add_conditional_edges(
        "generate_response",
        functools.partial(route_after_generate, allowed_tool_names=allowed),
        {"tools": "tools", "end": END},
    )
    graph.add_edge("tools", "generate_response")
    return graph.compile()
