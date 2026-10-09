"""Component 12 dependency injection: auth, ownership, app state.

Authentication is a single-owner bearer token bound to one
``owner_user_id`` (see :mod:`backend.api.settings`). Tokens compare
in constant time and are never logged. Ownership is always resolved
against durable rows — never trusted from request bodies.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Request

from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.requests.models import RequestRecord

from .errors import (
    AUTH_NOT_CONFIGURED,
    UNAUTHORIZED,
    ApiError,
)
from .settings import ApiSettings


def settings_of(request: Request) -> ApiSettings:
    """Settings stored on the application at creation."""
    settings = request.app.state.settings
    assert isinstance(settings, ApiSettings)
    return settings


def state_of(request: Request) -> Any:
    """Mutable application state (services, runtime, worker handles)."""
    return request.app.state.api


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def authenticate(request: Request) -> ApiSettings:
    """Require the configured bearer token (constant-time compare).

    Raises 503 when authentication is not configured, 401 for a
    missing or wrong token. The token value never appears in errors
    or logs.
    """
    settings = settings_of(request)
    if not settings.auth_configured or settings.api_token is None:
        status, code, message = AUTH_NOT_CONFIGURED
        raise ApiError(status, code, message)
    presented = _bearer_token(request)
    if presented is None or not secrets.compare_digest(
        presented, settings.api_token
    ):
        status, code, message = UNAUTHORIZED
        raise ApiError(status, code, message)
    return settings


async def owned_agent_ids(
    sessions: Any, owner_user_id: Any, *, limit: int = 500
) -> set[str]:
    """Agent ids durably owned by the authenticated user."""
    async with sessions() as session:
        rows = await AgentRepository(session).list_by_owner(
            owner_user_id, limit=limit
        )
        return {row.agent_id for row in rows}


async def require_owned_agent(
    sessions: Any,
    owner_user_id: Any,
    agent_id: str,
) -> Any:
    """Load one agent row iff the caller owns it (404 otherwise).

    Unknown and unowned agents are indistinguishable: existence
    never leaks across ownership boundaries.
    """
    async with sessions() as session:
        row = await AgentRepository(session).get(agent_id)
        if (
            row is None
            or row.owner_user_id != owner_user_id
            or not row.active
        ):
            raise ApiError(404, "UNKNOWN_AGENT", "agent not found")
        return row


async def owned_conversation_agent(
    sessions: Any,
    owner_user_id: Any,
    conversation_id: str,
) -> str:
    """Resolve a conversation to its agent iff the caller owns it.

    Garbage ids fail as invalid input; unknown conversations and
    conversations of other owners are indistinguishable (404). The
    owning service re-validates activity and membership at call time.
    """
    import uuid as uuid_mod

    from backend.persistence.repositories.conversations import (
        ConversationRepository,
    )

    try:
        cid = uuid_mod.UUID(conversation_id)
    except (ValueError, AttributeError, TypeError):
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    async with sessions() as session:
        row = await ConversationRepository(session).get(cid)
        if row is None:
            raise ApiError(
                404, "UNKNOWN_CONVERSATION", "conversation not found")
        agent = await AgentRepository(session).get(row.agent_id)
        if agent is None or agent.owner_user_id != owner_user_id:
            raise ApiError(
                404, "UNKNOWN_CONVERSATION", "conversation not found")
        return row.agent_id


async def request_involving_caller(
    sessions: Any,
    owner_user_id: Any,
    request_id: str,
) -> Any:
    """Load one request record iff it involves a caller-owned agent.

    Unknown requests and requests touching only other identities are
    indistinguishable (404 either way).
    """
    async with sessions() as session:
        row = await RemoteRequestRepository(session).get(request_id)
        if row is None:
            raise ApiError(404, "UNKNOWN_REQUEST", "request not found")
        record = RequestRecord.from_row(row)
    owned = await owned_agent_ids(sessions, owner_user_id)
    if (
        record.sender_agent_id not in owned
        and record.recipient_agent_id not in owned
    ):
        raise ApiError(404, "UNKNOWN_REQUEST", "request not found")
    return record
