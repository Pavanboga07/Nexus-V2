"""Approval data access (decision records only, no workflow)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Approval
from . import _flush


class ApprovalRepository:
    """Record and reload per-request approval decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        request_id: str,
        approving_agent_id: str,
        decision: str,
        reason: str | None = None,
    ) -> Approval:
        """Insert one decision row (at most one per request)."""
        approval = Approval(
            request_id=request_id,
            approving_agent_id=approving_agent_id,
            decision=decision,
            reason=reason,
        )
        self._session.add(approval)
        await _flush(self._session)
        return approval

    async def get_by_request_id(self, request_id: str) -> Approval | None:
        """Load the decision for a request, or None."""
        result = await self._session.execute(
            select(Approval).where(Approval.request_id == request_id)
        )
        return result.scalars().first()
