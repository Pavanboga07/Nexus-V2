"""Peer controls: inspect states and revoke authorization.

Peer rows are the authorization source for sending remote requests.
Revoking sets matching rows to ``revoked`` for every agent this owner
holds: the peer immediately loses the connected status the request
paths require. Removal is local — the remote side keeps its own rows
until its owner revokes there (there is no presence protocol to
notify through).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.api import deps
from backend.api.schemas import PeerRevokeResult
from backend.persistence.database import session_scope
from backend.persistence.repositories.peers import PeerRepository

router = APIRouter(tags=["peers"])


@router.post("/peers/{peer_agent_id}/revoke",
             response_model=PeerRevokeResult)
async def peer_revoke(
    peer_agent_id: str,
    request: Request,
    settings=Depends(deps.authenticate),
) -> PeerRevokeResult:
    """Revoke every local peering with one remote agent id."""
    state = deps.state_of(request)
    owned = await deps.owned_agent_ids(
        state.sessions, settings.owner_user_id)
    revoked = 0
    async with session_scope(state.sessions) as session:
        peers = PeerRepository(session)
        for local_agent_id in owned:
            row = await peers.get(local_agent_id, peer_agent_id)
            if row is not None and row.status != "revoked":
                from datetime import datetime, timezone

                await peers.revoke(
                    local_agent_id, peer_agent_id,
                    datetime.now(timezone.utc),
                )
                revoked += 1
    return PeerRevokeResult(revoked=revoked)
