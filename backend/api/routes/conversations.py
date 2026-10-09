"""Component 12 conversation endpoints (owner-bound, model-backed)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.agent.errors import AgentRuntimeError
from backend.api import deps
from backend.api.errors import MODEL_NOT_CONFIGURED, ApiError, to_api_error
from backend.api.schemas import (
    ConversationBody,
    ConversationCreate,
    ConversationMessageBody,
    TurnBody,
    TurnCreate,
    conversation_body,
    message_body,
)

router = APIRouter(tags=["conversations"])


def _conversations_of(request: Request):
    service = deps.state_of(request).conversations
    if service is None:
        status, code, message = MODEL_NOT_CONFIGURED
        raise ApiError(status, code, message)
    return service


@router.get("/conversations", response_model=list[ConversationBody])
async def list_conversations(
    agent_id: str,
    request: Request,
    settings=Depends(deps.authenticate),
    limit: int = 50,
    offset: int = 0,
) -> list[ConversationBody]:
    """Threads of one caller-owned agent (bounded window)."""
    if not 1 <= limit <= 200 or offset < 0 or offset > 1000:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    await deps.require_owned_agent(
        deps.state_of(request).sessions, settings.owner_user_id, agent_id)
    try:
        rows = await _conversations_of(request).list_conversations(
            agent_id, limit=limit, offset=offset)
    except AgentRuntimeError as exc:
        raise to_api_error(exc)
    return [conversation_body(row) for row in rows]


@router.post("/conversations", response_model=ConversationBody,
             status_code=201)
async def create_conversation(
    body: ConversationCreate,
    request: Request,
    settings=Depends(deps.authenticate),
) -> ConversationBody:
    """Open a thread for one caller-owned agent."""
    await deps.require_owned_agent(
        deps.state_of(request).sessions, settings.owner_user_id,
        body.agent_id)
    try:
        row = await _conversations_of(request).create_conversation(
            body.agent_id, title=body.title)
    except AgentRuntimeError as exc:
        raise to_api_error(exc)
    return conversation_body(row)


@router.get("/conversations/{conversation_id}/messages",
            response_model=list[ConversationMessageBody])
async def list_messages(
    conversation_id: str,
    request: Request,
    settings=Depends(deps.authenticate),
    limit: int = 50,
    offset: int = 0,
) -> list[ConversationMessageBody]:
    """Stored turns of one caller-owned conversation."""
    if not 1 <= limit <= 200 or offset < 0 or offset > 1000:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    state = deps.state_of(request)
    agent_id = await deps.owned_conversation_agent(
        state.sessions, settings.owner_user_id, conversation_id)
    try:
        service = _conversations_of(request)
        rows = await service.list_history(
            agent_id, _conversation_uuid(conversation_id),
            limit=limit + offset)
    except AgentRuntimeError as exc:
        raise to_api_error(exc)
    return [message_body(row) for row in rows[offset:offset + limit]]


@router.post("/conversations/{conversation_id}/turns",
             response_model=TurnBody)
async def submit_turn(
    conversation_id: str,
    body: TurnCreate,
    request: Request,
    settings=Depends(deps.authenticate),
) -> TurnBody:
    """Run one user turn through the agent runtime (persisted)."""
    state = deps.state_of(request)
    agent_id = await deps.owned_conversation_agent(
        state.sessions, settings.owner_user_id, conversation_id)
    try:
        turn = await _conversations_of(request).submit_turn(
            agent_id, _conversation_uuid(conversation_id), body.message)
    except AgentRuntimeError as exc:
        raise to_api_error(exc)
    return TurnBody(
        conversation_id=str(turn.conversation_id),
        user_message_id=turn.user_message_id,
        assistant_message_id=turn.assistant_message_id,
        answer=turn.response.text,
        model=turn.response.model_name,
    )


def _conversation_uuid(raw: str):
    """Parse a conversation id or fail as invalid input (never 500)."""
    import uuid as uuid_mod

    try:
        return uuid_mod.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
