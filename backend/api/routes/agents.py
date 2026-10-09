"""Component 12 agent and peer endpoints (caller-owned records only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.api import deps
from backend.api.schemas import AgentBody, PeerBody, agent_body
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=list[AgentBody])
async def list_agents(
    request: Request, settings=Depends(deps.authenticate),
) -> list[AgentBody]:
    """Local agents owned by the authenticated deployment."""
    state = deps.state_of(request)
    async with state.sessions() as session:
        rows = await AgentRepository(session).list_by_owner(
            settings.owner_user_id, limit=500
        )
        return [agent_body(row, owned=True) for row in rows]


@router.get("/agents/{agent_id}/peers", response_model=list[PeerBody])
async def list_peers(
    agent_id: str, request: Request, settings=Depends(deps.authenticate),
) -> list[PeerBody]:
    """Known peerings of one caller-owned agent (404 otherwise)."""
    state = deps.state_of(request)
    await deps.require_owned_agent(
        state.sessions, settings.owner_user_id, agent_id)
    async with state.sessions() as session:
        rows = await PeerRepository(session).list_for_local(
            agent_id, limit=500)
        return [
            PeerBody(peer_agent_id=row.peer_agent_id, status=row.status)
            for row in rows
        ]
