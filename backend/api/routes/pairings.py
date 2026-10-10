"""Pairing endpoints: invitations, acceptance, completion, links.

Invitations authorize first contact out-of-band; acceptance sends a
normal pairing-flagged request through RequestService (standard
delivery, registration, and approval); completion notifies the
initiator over the Gateway after B's explicit approval decision.
Secrets cross this boundary only inbound (validate/accept bodies);
they are never returned, logged, or echoed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request

from backend.api import deps
from backend.api.errors import ApiError, to_api_error
from backend.api.events import PAIRING_UPDATED, make_event
from backend.api.schemas import (
    InvitationBody,
    InvitationCreate,
    InvitationCreated,
    InvitationInfo,
    InvitationValidate,
    PairingAccept,
    PairingBody,
    PairingComplete,
    PairingCompletion,
)
from backend.pairing import invitation
from backend.pairing.errors import PairingError
from backend.pairing.service import PairingService
from backend.pairing.store import PairingStore

router = APIRouter(tags=["pairings"])


def _utcnow():
    return datetime.now(timezone.utc)


def _service_of(request: Request, agent_id: str) -> PairingService:
    state = deps.state_of(request)
    return PairingService(
        state.sessions, agent_id, state.identities)


def _invitation_state(row: Any, now: Any) -> tuple[bool, bool]:
    expires_at = row.expires_at
    expired = expires_at.tzinfo is None or now >= expires_at
    return row.used_at is not None or expired, row.revoked_at is not None


@router.post("/pairings/invitations", response_model=InvitationCreated,
              status_code=201)
async def invitation_create(
    body: InvitationCreate,
    request: Request,
    settings=Depends(deps.authenticate),
) -> InvitationCreated:
    """Issue one single-use invitation for an owned agent."""
    state = deps.state_of(request)
    await deps.require_owned_agent(
        state.sessions, settings.owner_user_id, body.agent_id)
    service = _service_of(request, body.agent_id)
    try:
        created = await service.create_invitation(
            body.agent_id, body.ttl_seconds, _utcnow())
    except PairingError as exc:
        raise to_api_error(exc)
    return InvitationCreated(
        invitation_id=created["invitation_id"],
        invitation=created["invitation"],
        secret=created["secret"],
        expires_at=created["expires_at"],
    )


@router.get("/pairings/invitations", response_model=list[InvitationBody])
async def invitation_list(
    request: Request,
    settings=Depends(deps.authenticate),
    agent_id: str = "",
) -> list[InvitationBody]:
    """List one owned agent's invitations with live states."""
    state = deps.state_of(request)
    if not agent_id:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    await deps.require_owned_agent(
        state.sessions, settings.owner_user_id, agent_id)
    store = PairingStore(state.sessions)
    rows = await store.list_invitations(agent_id)
    now = _utcnow()
    bodies = []
    for row in rows:
        used, revoked = _invitation_state(row, now)
        bodies.append(InvitationBody(
            invitation_id=row.invitation_id,
            issuer_agent_id=row.issuer_agent_id,
            expires_at=row.expires_at.isoformat(),
            used=used,
            revoked=revoked,
        ))
    return bodies


@router.post("/pairings/invitations/{invitation_id}/revoke")
async def invitation_revoke(
    invitation_id: str,
    request: Request,
    settings=Depends(deps.authenticate),
) -> dict:
    """Revoke an invitation (blocks new uses, not decided pairings)."""
    state = deps.state_of(request)
    store = PairingStore(state.sessions)
    row = await store.get_invitation(invitation_id)
    if row is None:
        raise ApiError(
            404, "PAIRING_NOT_FOUND",
            "pairing invitation or link not found")
    await deps.require_owned_agent(
        state.sessions, settings.owner_user_id, row.issuer_agent_id)
    revoked = await store.revoke_invitation(invitation_id, _utcnow())
    return {"revoked": revoked}


@router.post("/pairings/validate", response_model=InvitationInfo)
async def invitation_validate(
    body: InvitationValidate,
    request: Request,
    settings=Depends(deps.authenticate),
) -> InvitationInfo:
    """Check an invitation's cryptography and expiry (no state change).

    Revocation and single-use are enforced where they matter (B-side
    registration and completion), because only B's store knows them.
    """
    _ = (request, settings)
    try:
        fields, _secret = invitation.split_code(body.invitation)
    except PairingError as exc:
        raise to_api_error(exc)
    return InvitationInfo(
        agent_id=fields["agent_id"],
        public_key=fields["public_key"],
        fingerprint=invitation.fingerprint(fields["public_key"]),
        expires_at=fields["expires_at"],
    )


@router.post("/pairings/accept", response_model=PairingBody,
              status_code=201)
async def pairing_accept(
    body: PairingAccept,
    request: Request,
    settings=Depends(deps.authenticate),
) -> PairingBody:
    """Verify an invitation and send the pairing request (User A)."""
    state = deps.state_of(request)
    await deps.require_owned_agent(
        state.sessions, settings.owner_user_id, body.sender_agent_id)
    service = _service_of(request, body.sender_agent_id)
    try:
        accepted = await service.accept_invitation(
            body.invitation, body.sender_agent_id, _utcnow())
    except PairingError as exc:
        raise to_api_error(exc)
    except Exception as exc:
        from backend.requests.errors import RequestServiceError

        if isinstance(exc, RequestServiceError):
            raise to_api_error(exc)
        raise
    deps.state_of(request).events.publish(make_event(PAIRING_UPDATED, {
        "pairing_id": accepted["pairing_id"],
        "request_id": accepted["request_id"],
        "status": "pending",
    }))
    return PairingBody(
        pairing_id=accepted["pairing_id"],
        local_agent_id=body.sender_agent_id,
        remote_agent_id=accepted["remote_agent_id"],
        status="pending",
        invitation_id=accepted["invitation_id"],
        request_id=accepted["request_id"],
    )


@router.get("/pairings", response_model=list[PairingBody])
async def pairing_list(
    request: Request,
    settings=Depends(deps.authenticate),
    agent_id: str = "",
) -> list[PairingBody]:
    """One owned agent's pairing links with stored states."""
    state = deps.state_of(request)
    if not agent_id:
        raise ApiError(422, "INVALID_INPUT", "request input is invalid")
    await deps.require_owned_agent(
        state.sessions, settings.owner_user_id, agent_id)
    rows = await PairingStore(state.sessions).list_links(agent_id)
    return [
        PairingBody(
            pairing_id=str(row.id),
            local_agent_id=row.local_agent_id,
            remote_agent_id=row.remote_agent_id,
            status=row.status,
            invitation_id=row.invitation_id,
            request_id=row.request_id,
        )
        for row in rows
    ]


@router.post("/pairings/complete", response_model=PairingCompletion)
async def pairing_complete(
    body: PairingComplete,
    request: Request,
    settings=Depends(deps.authenticate),
) -> PairingCompletion:
    """Notify the initiator after this side's explicit decision (B).

    The decision comes from persisted request state (APPROVED or
    REJECTED via the standard endpoints) — never from a client claim.
    Retrying is safe: rows are create-or-update and the acceptance is
    re-sent until the Gateway acknowledges it.
    """
    from backend.requests.service import RequestService

    state = deps.state_of(request)
    service = RequestService(state.sessions, state.identities)
    record = await service.get_request(body.request_id)
    if record is None:
        raise ApiError(404, "PAIRING_NOT_FOUND",
                       "pairing invitation or link not found")
    owned = await deps.owned_agent_ids(
        state.sessions, settings.owner_user_id)
    if record.recipient_agent_id not in owned:
        raise ApiError(403, "FORBIDDEN",
                       "not authorized for this resource")
    client = state.gateway_clients.get(record.recipient_agent_id)
    if client is None:
        raise ApiError(503, "PAIRING_TRANSPORT",
                       "the pairing message could not be delivered; "
                       "it is safe to retry")
    pairing = PairingService(
        state.sessions, record.recipient_agent_id, state.identities)
    try:
        outcome = await pairing.complete_pairing(
            body.request_id, record.recipient_agent_id, client,
            _utcnow())
    except PairingError as exc:
        raise to_api_error(exc)
    return PairingCompletion(
        pairing_id=outcome["pairing_id"],
        approved=outcome["approved"],
        message_id=outcome["message_id"],
        ack=outcome["ack"],
    )
