"""Provider-credential and pairing data access (no crypto decisions)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PairInvitation, PairLink, ProviderCredential
from . import _flush


class ProviderCredentialRepository:
    """One encrypted provider configuration per owner."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, owner_user_id: UUID) -> ProviderCredential | None:
        """Load the owner's configuration, or None when absent."""
        result = await self._session.execute(
            select(ProviderCredential).where(
                ProviderCredential.owner_user_id == owner_user_id
            )
        )
        return result.scalars().first()

    async def upsert(
        self,
        owner_user_id: UUID,
        provider: str,
        model: str,
        key_encrypted: bytes,
    ) -> ProviderCredential:
        """Create or replace the owner's configuration atomically."""
        row = await self.get(owner_user_id)
        if row is None:
            row = ProviderCredential(
                owner_user_id=owner_user_id,
                provider=provider,
                model=model,
                key_encrypted=key_encrypted,
            )
            self._session.add(row)
        else:
            row.provider = provider
            row.model = model
            row.key_encrypted = key_encrypted
        await _flush(self._session)
        return row

    async def delete(self, owner_user_id: UUID) -> bool:
        """Remove the owner's configuration; True when one existed."""
        row = await self.get(owner_user_id)
        if row is None:
            return False
        await self._session.delete(row)
        await _flush(self._session)
        return True


class PairInvitationRepository:
    """Issued pairing invitations (B side)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        invitation_id: str,
        issuer_agent_id: str,
        secret_sha256: str,
        expires_at: datetime,
    ) -> PairInvitation:
        """Insert one invitation row."""
        row = PairInvitation(
            invitation_id=invitation_id,
            issuer_agent_id=issuer_agent_id,
            secret_sha256=secret_sha256,
            expires_at=expires_at,
        )
        self._session.add(row)
        await _flush(self._session)
        return row

    async def get(self, invitation_id: str) -> PairInvitation | None:
        """Load one invitation by id, or None."""
        result = await self._session.execute(
            select(PairInvitation).where(
                PairInvitation.invitation_id == invitation_id
            )
        )
        return result.scalars().first()

    async def list_for_issuer(
        self, issuer_agent_id: str, *, limit: int = 100
    ) -> list[PairInvitation]:
        """Invitations issued by one agent, newest first."""
        result = await self._session.execute(
            select(PairInvitation)
            .where(PairInvitation.issuer_agent_id == issuer_agent_id)
            .order_by(PairInvitation.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def mark_used(
        self, invitation_id: str, used_at: datetime
    ) -> PairInvitation | None:
        """Record single use; None when the row is missing."""
        row = await self.get(invitation_id)
        if row is None:
            return None
        row.used_at = used_at
        await _flush(self._session)
        return row

    async def revoke(
        self, invitation_id: str, revoked_at: datetime
    ) -> PairInvitation | None:
        """Record revocation; None when the row is missing."""
        row = await self.get(invitation_id)
        if row is None:
            return None
        row.revoked_at = revoked_at
        await _flush(self._session)
        return row


class PairLinkRepository:
    """Each side's view of pairing handshakes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        local_agent_id: str,
        remote_agent_id: str,
        remote_public_key: str,
        status: str = "pending",
        invitation_id: str | None = None,
        request_id: str | None = None,
    ) -> PairLink:
        """Insert one link row."""
        row = PairLink(
            local_agent_id=local_agent_id,
            remote_agent_id=remote_agent_id,
            remote_public_key=remote_public_key,
            status=status,
            invitation_id=invitation_id,
            request_id=request_id,
        )
        self._session.add(row)
        await _flush(self._session)
        return row

    async def list_for_local(
        self, local_agent_id: str, *, limit: int = 100
    ) -> list[PairLink]:
        """One agent's links, newest first."""
        result = await self._session.execute(
            select(PairLink)
            .where(PairLink.local_agent_id == local_agent_id)
            .order_by(PairLink.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def find_open(
        self, local_agent_id: str, remote_agent_id: str
    ) -> PairLink | None:
        """Newest non-terminal link for a pair, or None."""
        result = await self._session.execute(
            select(PairLink)
            .where(
                PairLink.local_agent_id == local_agent_id,
                PairLink.remote_agent_id == remote_agent_id,
                PairLink.status == "pending",
            )
            .order_by(PairLink.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def set_status(
        self, link_id: Any, status: str
    ) -> PairLink | None:
        """Transition one link; None when the row is missing."""
        result = await self._session.execute(
            select(PairLink).where(PairLink.id == link_id)
        )
        row = result.scalars().first()
        if row is None:
            return None
        row.status = status
        await _flush(self._session)
        return row
