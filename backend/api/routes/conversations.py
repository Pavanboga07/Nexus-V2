"""Component 12 conversation endpoints (owner-bound, model-backed)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from backend.agent.errors import AgentRuntimeError
from backend.api import deps
from backend.api.effective_runtime import (
    effective_runtime as _effective_runtime_of,
)
from backend.api.errors import MODEL_NOT_CONFIGURED, ApiError, to_api_error
from backend.api.events import TURN_COMPLETED, make_event
from backend.provider_config.errors import ProviderConfigError
from backend.api.schemas import (
    ConversationBody,
    ConversationCreate,
    ConversationMessageBody,
    ConversationUpdate,
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


def _turn_service(request: Request, runtime):
    """Conversation service for this turn, even when startup had no model.

    A transient service still gives atomic transcript writes; the
    per-conversation locks apply to the shared instance.
    """
    try:
        return _conversations_of(request)
    except ApiError:
        from backend.agent.conversation_service import (
            ConversationService as _ConversationService,
        )

        return _ConversationService(deps.state_of(request).sessions, runtime)


@router.get("/conversations", response_model=list[ConversationBody])
async def list_conversations(
    agent_id: str,
    request: Request,
    settings=Depends(deps.authenticate),
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
) -> list[ConversationBody]:
    """Threads of one caller-owned agent (pinned first, searchable)."""
    if not 1 <= limit <= 200 or offset < 0 or offset > 1000:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    if q is not None and len(q) > 200:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    await deps.require_owned_agent(
        deps.state_of(request).sessions, settings.owner_user_id, agent_id)
    try:
        rows = await _conversations_of(request).list_conversations(
            agent_id, limit=limit, offset=offset, query=q)
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


@router.patch("/conversations/{conversation_id}",
              response_model=ConversationBody)
async def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    request: Request,
    settings=Depends(deps.authenticate),
) -> ConversationBody:
    """Rename or pin one caller-owned conversation."""
    if body.title is None and body.pinned is None:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    state = deps.state_of(request)
    agent_id = await deps.owned_conversation_agent(
        state.sessions, settings.owner_user_id, conversation_id)
    service = _conversations_of(request)
    try:
        row = None
        if body.title is not None:
            row = await service.rename_conversation(
                agent_id, _conversation_uuid(conversation_id), body.title)
        if body.pinned is not None:
            row = await service.set_conversation_pinned(
                agent_id, _conversation_uuid(conversation_id), body.pinned)
    except AgentRuntimeError as exc:
        raise to_api_error(exc)
    assert row is not None
    return conversation_body(row)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    request: Request,
    settings=Depends(deps.authenticate),
) -> dict[str, bool]:
    """Delete one caller-owned conversation and its messages."""
    state = deps.state_of(request)
    agent_id = await deps.owned_conversation_agent(
        state.sessions, settings.owner_user_id, conversation_id)
    try:
        await _conversations_of(request).delete_conversation(
            agent_id, _conversation_uuid(conversation_id))
    except AgentRuntimeError as exc:
        raise to_api_error(exc)
    return {"deleted": True}


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
        runtime = await _effective_runtime_of(
            request, settings.owner_user_id)
    except ProviderConfigError as exc:
        raise to_api_error(exc)
    if runtime is None:
        status, code, message = MODEL_NOT_CONFIGURED
        raise ApiError(status, code, message)
    try:
        service = _turn_service(request, runtime)
        turn = await service.submit_turn(
            agent_id, _conversation_uuid(conversation_id), body.message,
            runtime=runtime)
    except AgentRuntimeError as exc:
        raise to_api_error(exc)
    deps.state_of(request).events.publish(make_event(TURN_COMPLETED, {
        "conversation_id": str(turn.conversation_id),
        "agent_id": agent_id,
        "user_message_id": turn.user_message_id,
        "assistant_message_id": turn.assistant_message_id,
        "model": turn.response.model_name,
    }))
    return TurnBody(
        conversation_id=str(turn.conversation_id),
        user_message_id=turn.user_message_id,
        assistant_message_id=turn.assistant_message_id,
        answer=turn.response.text,
        model=turn.response.model_name,
    )


@router.post("/conversations/{conversation_id}/turns/stream")
async def stream_turn(
    conversation_id: str,
    body: TurnCreate,
    request: Request,
    settings=Depends(deps.authenticate),
):
    """Stream one model turn as Server-Sent Events.

    Events are ``user``, zero or more ``delta``, then ``done``; failures
    after the stream starts arrive as ``error``. A client abort stops
    generation and stores any partial answer.
    """
    state = deps.state_of(request)
    agent_id = await deps.owned_conversation_agent(
        state.sessions, settings.owner_user_id, conversation_id)
    try:
        runtime = await _effective_runtime_of(
            request, settings.owner_user_id)
    except ProviderConfigError as exc:
        raise to_api_error(exc)
    if runtime is None:
        status, code, message = MODEL_NOT_CONFIGURED
        raise ApiError(status, code, message)
    service = _turn_service(request, runtime)

    async def events():
        stream = service.stream_turn(
            agent_id, _conversation_uuid(conversation_id), body.message,
            runtime=runtime)
        try:
            async for event in stream:
                if await request.is_disconnected():
                    break
                if event["type"] == "done":
                    state.events.publish(make_event(TURN_COMPLETED, {
                        "conversation_id": event["conversation_id"],
                        "agent_id": agent_id,
                        "user_message_id": event["user_message_id"],
                        "assistant_message_id": event["assistant_message_id"],
                        "model": event["model"],
                    }))
                payload = json.dumps(event, separators=(",", ":"))
                yield f"event: {event['type']}\ndata: {payload}\n\n"
        except AgentRuntimeError as exc:
            api = to_api_error(exc)
            payload = json.dumps(
                {"code": api.code, "message": api.message},
                separators=(",", ":"),
            )
            yield f"event: error\ndata: {payload}\n\n"
        finally:
            await stream.aclose()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def _conversation_uuid(raw: str):
    """Parse a conversation id or fail as invalid input (never 500)."""
    import uuid as uuid_mod

    try:
        return uuid_mod.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
