"""Component 12 remote-request, approval, and execution endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request

from backend.api import deps
from backend.api.errors import (
    EXECUTION_NOT_CONFIGURED,
    SIGNING_NOT_CONFIGURED,
    ApiError,
    to_api_error,
)
from backend.api.schemas import (
    ApprovalAction,
    ExecutionBody,
    RequestBody,
    RequestCreate,
    request_body,
)
from backend.execution.executor import RemoteExecutor
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.requests.errors import RequestServiceError
from backend.requests.models import RequestRecord
from backend.requests.service import RequestService

router = APIRouter(tags=["requests"])

_LIST_LIMIT = 200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _answer_for(
    sessions, response_message_id: str | None
) -> str | None:
    """Stored answer text for a terminal request (owner-safe payload)."""
    if response_message_id is None:
        return None
    async with sessions() as session:
        row = await MessageRepository(session).get(response_message_id)
        if row is None:
            return None
        payload = row.envelope.get("payload", {})
        if not isinstance(payload, dict):
            return None
        answer = payload.get("answer")
        return answer if isinstance(answer, str) and answer else None


@router.post("/requests", response_model=RequestBody, status_code=201)
async def create_request(
    body: RequestCreate,
    request: Request,
    settings=Depends(deps.authenticate),
) -> RequestBody:
    """Create and durably queue one remote request (no network yet)."""
    state = deps.state_of(request)
    if state.identities is None:
        status, code, message = SIGNING_NOT_CONFIGURED
        raise ApiError(status, code, message)
    owned = await deps.owned_agent_ids(
        state.sessions, settings.owner_user_id)
    if body.sender_agent_id not in owned:
        raise ApiError(404, "UNKNOWN_AGENT", "agent not found")
    try:
        record = await RequestService(
            state.sessions, state.identities
        ).create_remote_request(
            body.sender_agent_id,
            body.recipient_agent_id,
            body.query,
            list(body.requested_capabilities),
            _utcnow() + timedelta(seconds=body.expires_in_seconds),
            request_id=body.request_id,
            message_id=body.message_id,
            now=_utcnow(),
        )
    except RequestServiceError as exc:
        raise to_api_error(exc)
    return request_body(record)


@router.get("/requests", response_model=list[RequestBody])
async def list_requests(
    request: Request,
    settings=Depends(deps.authenticate),
    role: str = "outbound",
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RequestBody]:
    """Bounded requests involving the caller's agents.

    ``role=outbound`` lists requests the caller's agents sent;
    ``role=inbound`` lists requests addressed to them.
    """
    if role not in ("outbound", "inbound"):
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    if not 1 <= limit <= 200 or offset < 0 or offset > 1000:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    state = deps.state_of(request)
    owned = await deps.owned_agent_ids(
        state.sessions, settings.owner_user_id)
    seen: dict[str, RequestBody] = {}
    async with state.sessions() as session:
        repo = RemoteRequestRepository(session)
        for agent_id in sorted(owned):
            rows = await (
                repo.list_by_sender(agent_id, limit=_LIST_LIMIT)
                if role == "outbound"
                else repo.list_by_recipient(agent_id, limit=_LIST_LIMIT)
            )
            for row in rows:
                if status is not None and row.status != status:
                    continue
                if row.request_id not in seen:
                    seen[row.request_id] = request_body(
                        RequestRecord.from_row(row))
    ordered = sorted(seen.values(), key=lambda item: item.request_id)
    return ordered[offset:offset + limit]


@router.get("/requests/{request_id}", response_model=RequestBody)
async def get_request(
    request_id: str,
    request: Request,
    settings=Depends(deps.authenticate),
) -> RequestBody:
    """Status, parties, and safe result metadata for one request."""
    state = deps.state_of(request)
    record = await deps.request_involving_caller(
        state.sessions, settings.owner_user_id, request_id)
    answer = None
    if record.status in ("COMPLETED", "FAILED"):
        answer = await _answer_for(
            state.sessions, record.response_message_id)
    return request_body(record, answer=answer)


@router.get("/approvals/pending", response_model=list[RequestBody])
async def list_pending_approvals(
    request: Request,
    settings=Depends(deps.authenticate),
    limit: int = 50,
    offset: int = 0,
) -> list[RequestBody]:
    """Inbound requests awaiting the caller's approval decision."""
    if not 1 <= limit <= 200 or offset < 0 or offset > 1000:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    state = deps.state_of(request)
    owned = await deps.owned_agent_ids(
        state.sessions, settings.owner_user_id)
    found: list[RequestBody] = []
    async with state.sessions() as session:
        repo = RemoteRequestRepository(session)
        for agent_id in sorted(owned):
            for row in await repo.list_by_recipient(
                agent_id, limit=_LIST_LIMIT
            ):
                if row.status != "WAITING_APPROVAL":
                    continue
                found.append(request_body(RequestRecord.from_row(row)))
    found.sort(key=lambda item: item.request_id)
    return found[offset:offset + limit]


async def _decide(
    request: Request,
    settings,
    request_id: str,
    body: ApprovalAction,
    approve: bool,
) -> RequestBody:
    state = deps.state_of(request)
    record = await deps.request_involving_caller(
        state.sessions, settings.owner_user_id, request_id)
    owned = await deps.owned_agent_ids(
        state.sessions, settings.owner_user_id)
    # The decision is recorded as the persisted recipient — never a
    # caller-supplied identity.
    if record.recipient_agent_id not in owned:
        raise ApiError(403, "FORBIDDEN",
                       "not authorized for this resource")
    try:
        service = RequestService(state.sessions, state.identities)
        if approve:
            updated = await service.approve(
                request_id, record.recipient_agent_id,
                reason=body.reason,
            )
        else:
            updated = await service.reject(
                request_id, record.recipient_agent_id,
                reason=body.reason,
            )
    except RequestServiceError as exc:
        raise to_api_error(exc)
    return request_body(updated)


@router.post("/requests/{request_id}/approve", response_model=RequestBody)
async def approve_request(
    request_id: str,
    body: ApprovalAction,
    request: Request,
    settings=Depends(deps.authenticate),
) -> RequestBody:
    """Approve an inbound request as its persisted recipient."""
    return await _decide(request, settings, request_id, body, True)


@router.post("/requests/{request_id}/reject", response_model=RequestBody)
async def reject_request(
    request_id: str,
    body: ApprovalAction,
    request: Request,
    settings=Depends(deps.authenticate),
) -> RequestBody:
    """Reject an inbound request as its persisted recipient."""
    return await _decide(request, settings, request_id, body, False)


@router.post("/requests/{request_id}/execute",
             response_model=ExecutionBody)
async def execute_request(
    request_id: str,
    request: Request,
    settings=Depends(deps.authenticate),
) -> ExecutionBody:
    """Execute an approved inbound request (no approval assertion).

    The executor re-verifies persisted approval, recipient binding,
    and expiry itself; this endpoint only checks that the caller owns
    the recipient side before invoking it.
    """
    state = deps.state_of(request)
    if state.runtime is None or state.identities is None:
        status, code, message = EXECUTION_NOT_CONFIGURED
        raise ApiError(status, code, message)
    record = await deps.request_involving_caller(
        state.sessions, settings.owner_user_id, request_id)
    owned = await deps.owned_agent_ids(
        state.sessions, settings.owner_user_id)
    if record.recipient_agent_id not in owned:
        raise ApiError(403, "FORBIDDEN",
                       "not authorized for this resource")
    # APPROVED runs; PROCESSING replays the live claim as
    # IN_PROGRESS via the executor. Anything else (including
    # terminal states) is rejected before invoking it.
    if record.status not in ("APPROVED", "PROCESSING"):
        raise ApiError(409, "ILLEGAL_TRANSITION",
                       "operation not allowed in the current state")
    try:
        outcome = await RemoteExecutor(
            state.sessions,
            record.recipient_agent_id,
            state.identities,
            state.runtime,
        ).execute(request_id)
    except RequestServiceError as exc:
        raise to_api_error(exc)
    return ExecutionBody(
        request_id=outcome.request_id,
        status=outcome.status,
        executed=outcome.executed,
        response_message_id=outcome.response_message_id,
        detail=outcome.record.status,
    )
