"""Pairing persistence: invitations and links (no crypto here).

Single-use redemption runs inside the caller's transaction via
``redeem_for_registration`` (row lock + mark), so the inbound
processor can burn an invitation atomically with registration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from backend.persistence.database import session_scope
from backend.persistence.repositories.pairing import (
    PairInvitationRepository,
    PairLinkRepository,
)


class PairingStore:
    """CRUD for invitations and links over one session factory."""

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    async def create_invitation(
        self,
        invitation_id: str,
        issuer_agent_id: str,
        secret_sha256: str,
        expires_at: datetime,
    ):
        """Insert one invitation row."""
        async with session_scope(self._sessions) as session:
            return await PairInvitationRepository(session).create(
                invitation_id, issuer_agent_id, secret_sha256,
                expires_at,
            )

    async def get_invitation(self, invitation_id: str):
        """Load one invitation by id, or None."""
        async with session_scope(self._sessions) as session:
            return await PairInvitationRepository(session).get(
                invitation_id)

    async def list_invitations(self, issuer_agent_id: str):
        """One issuer's invitations, newest first."""
        async with session_scope(self._sessions) as session:
            return await PairInvitationRepository(session).list_for_issuer(
                issuer_agent_id)

    async def revoke_invitation(
        self, invitation_id: str, now: datetime
    ) -> bool:
        """Mark revoked; False when the row is missing."""
        async with session_scope(self._sessions) as session:
            row = await PairInvitationRepository(session).revoke(
                invitation_id, now)
            return row is not None

    async def redeem_for_registration(
        self,
        session: Any,
        invitation_id: str,
        secret: str,
        issuer_agent_id: str,
        now: datetime,
    ) -> bool:
        """Atomically burn one invitation inside an open transaction.

        Returns True only when the row exists, names this issuer, is
        unexpired, unrevoked, unused, and the secret matches — and marks
        it used as part of the same transaction. Callers pass the
        session they will commit with registration, so double use races
        serialize on the row lock.
        """
        from backend.pairing.invitation import secret_matches
        from backend.persistence.models import PairInvitation

        locked = await session.execute(
            select(PairInvitation)
            .where(PairInvitation.invitation_id == invitation_id)
            .with_for_update()
        )
        row = locked.scalars().first()
        if row is None:
            return False
        if row.issuer_agent_id != issuer_agent_id:
            return False
        if row.revoked_at is not None or row.used_at is not None:
            return False
        if row.expires_at.tzinfo is None:
            return False
        if now >= row.expires_at:
            return False
        if not secret_matches(secret, row.secret_sha256):
            return False
        row.used_at = now
        return True

    async def create_link(
        self,
        local_agent_id: str,
        remote_agent_id: str,
        remote_public_key: str,
        invitation_id: str | None = None,
        request_id: str | None = None,
    ):
        """Insert one link row (default status pending)."""
        async with session_scope(self._sessions) as session:
            return await PairLinkRepository(session).create(
                local_agent_id, remote_agent_id, remote_public_key,
                invitation_id=invitation_id, request_id=request_id,
            )

    async def list_links(self, local_agent_id: str):
        """One agent's links, newest first."""
        async with session_scope(self._sessions) as session:
            return await PairLinkRepository(session).list_for_local(
                local_agent_id)

    async def find_open_link(
        self, local_agent_id: str, remote_agent_id: str
    ):
        """Newest pending link for a pair, or None."""
        async with session_scope(self._sessions) as session:
            return await PairLinkRepository(session).find_open(
                local_agent_id, remote_agent_id)

    async def set_link_status(self, link_id: UUID, status: str) -> bool:
        """Transition one link; False when the row is missing."""
        async with session_scope(self._sessions) as session:
            row = await PairLinkRepository(session).set_status(
                link_id, status)
            return row is not None
