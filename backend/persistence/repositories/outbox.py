"""Outbox data access (queue state only; worker lives in backend/worker).

``claim_pending`` is the worker's atomic-claim primitive: SELECT …
FOR UPDATE SKIP LOCKED moves rows to PROCESSING inside the caller's
transaction, so concurrent workers never own the same row. Outcome
methods with an ``expected_token`` guard only complete the claim they
were issued, so a stale worker can never overwrite a newer claim.

Worker paths MUST use the token-guarded outcome methods
(``mark_sent_if_claimed``, ``mark_failed_if_claimed``,
``mark_uncertain_if_claimed``, ``reschedule_if_claimed``,
``set_transmission_started``). The unguarded ``mark_sent`` /
``mark_failed`` exist only for non-claim flows (tests, tooling,
reconciliation); using them on a claimed row would let a stale worker
overwrite its successor's result.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import OutboxMessage
from . import _flush

_MAX_ERROR_LENGTH = 1024


class OutboxRepository:
    """Create, inspect, and advance outbox rows (no dispatch here)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, message_id: str, available_at: datetime
    ) -> OutboxMessage:
        """Queue one message for dispatch (PENDING, zero attempts).

        ``available_at`` is required and must be timezone-aware: the
        queue window is meaningless without an explicit instant.
        """
        row = OutboxMessage(message_id=message_id, available_at=available_at)
        self._session.add(row)
        await _flush(self._session)
        return row

    async def get(self, message_id: str) -> OutboxMessage | None:
        """Load one outbox row by message id, or None."""
        result = await self._session.execute(
            select(OutboxMessage).where(
                OutboxMessage.message_id == message_id
            )
        )
        return result.scalars().first()

    async def get_pending(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> list[OutboxMessage]:
        """PENDING rows whose window has arrived, oldest first (read-only)."""
        current = now if now is not None else datetime.now(timezone.utc)
        result = await self._session.execute(
            select(OutboxMessage)
            .where(
                OutboxMessage.status == "PENDING",
                OutboxMessage.available_at <= current,
            )
            .order_by(OutboxMessage.available_at, OutboxMessage.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def claim_pending(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
        lease_seconds: float = 120.0,
    ) -> list[OutboxMessage]:
        """Atomically claim due rows into PROCESSING (worker primitive).

        Row locks with SKIP LOCKED mean concurrent claimants never
        receive the same row. Each claim carries a unique token, a
        lease expiry, and an incremented attempt count; later outcome
        writes must present the same token.
        """
        current = now if now is not None else datetime.now(timezone.utc)
        result = await self._session.execute(
            select(OutboxMessage)
            .where(
                OutboxMessage.status == "PENDING",
                OutboxMessage.available_at <= current,
            )
            .order_by(OutboxMessage.available_at, OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed = list(result.scalars().all())
        for row in claimed:
            row.status = "PROCESSING"
            row.attempts = (row.attempts or 0) + 1
            row.claim_token = uuid.uuid4().hex
            row.lease_expires_at = current + timedelta(seconds=lease_seconds)
            row.transmission_started = False
        if claimed:
            await _flush(self._session)
        return claimed

    async def set_transmission_started(
        self, message_id: str, claim_token: str
    ) -> bool:
        """Record that bytes may be on the wire (crash-safety marker).

        Called immediately before the first send attempt so a crash
        afterwards recovers to UNCERTAIN instead of a blind PENDING
        retry. Returns False unless the row is still PROCESSING under
        the same token.
        """
        row = await self.get(message_id)
        if (
            row is None
            or row.status != "PROCESSING"
            or row.claim_token != claim_token
        ):
            return False
        row.transmission_started = True
        await _flush(self._session)
        return True

    async def mark_sent_if_claimed(
        self, message_id: str, claim_token: str, sent_at: datetime
    ) -> OutboxMessage | None:
        """Complete a claim as SENT; None unless token still owns it."""
        row = await self.get(message_id)
        if (
            row is None
            or row.status != "PROCESSING"
            or row.claim_token != claim_token
        ):
            return None
        row.status = "SENT"
        row.sent_at = sent_at
        await _flush(self._session)
        return row

    async def mark_failed_if_claimed(
        self,
        message_id: str,
        claim_token: str,
        error: str,
        available_at: datetime | None = None,
    ) -> OutboxMessage | None:
        """Complete a claim as FAILED; None unless token still owns it."""
        row = await self.get(message_id)
        if (
            row is None
            or row.status != "PROCESSING"
            or row.claim_token != claim_token
        ):
            return None
        row.status = "FAILED"
        row.last_error = error[:_MAX_ERROR_LENGTH]
        if available_at is not None:
            row.available_at = available_at
        await _flush(self._session)
        return row

    async def mark_uncertain_if_claimed(
        self, message_id: str, claim_token: str, detail: str = ""
    ) -> OutboxMessage | None:
        """Park an ambiguous claim as UNCERTAIN (never auto-retried)."""
        row = await self.get(message_id)
        if (
            row is None
            or row.status != "PROCESSING"
            or row.claim_token != claim_token
        ):
            return None
        row.status = "UNCERTAIN"
        if detail:
            row.last_error = detail[:_MAX_ERROR_LENGTH]
        await _flush(self._session)
        return row

    async def reschedule_if_claimed(
        self, message_id: str, claim_token: str, available_at: datetime
    ) -> OutboxMessage | None:
        """Return a claim to PENDING for a future attempt (safe retry)."""
        row = await self.get(message_id)
        if (
            row is None
            or row.status != "PROCESSING"
            or row.claim_token != claim_token
        ):
            return None
        row.status = "PENDING"
        row.available_at = available_at
        row.claim_token = None
        row.lease_expires_at = None
        row.transmission_started = False
        await _flush(self._session)
        return row

    async def recover_stale_claims(
        self, now: datetime | None = None
    ) -> list[OutboxMessage]:
        """Recover PROCESSING rows whose lease expired.

        A stale claim that never started transmission returns to
        PENDING (safe retry); one that may have transmitted becomes
        UNCERTAIN (preserved for reconciliation, never auto-retried).
        """
        current = now if now is not None else datetime.now(timezone.utc)
        result = await self._session.execute(
            select(OutboxMessage)
            .where(
                OutboxMessage.status == "PROCESSING",
                or_(
                    OutboxMessage.lease_expires_at.is_(None),
                    OutboxMessage.lease_expires_at <= current,
                ),
            )
            .with_for_update(skip_locked=True)
        )
        recovered = list(result.scalars().all())
        for row in recovered:
            if row.transmission_started:
                row.status = "UNCERTAIN"
            else:
                row.status = "PENDING"
                row.claim_token = None
                row.lease_expires_at = None
        if recovered:
            await _flush(self._session)
        return recovered

    async def mark_sent(
        self, message_id: str, sent_at: datetime
    ) -> OutboxMessage | None:
        """Record successful dispatch."""
        row = await self.get(message_id)
        if row is None:
            return None
        row.status = "SENT"
        row.sent_at = sent_at
        await _flush(self._session)
        return row

    async def mark_failed(
        self,
        message_id: str,
        error: str,
        available_at: datetime | None = None,
    ) -> OutboxMessage | None:
        """Record a failed attempt (bounded error text, optional retry)."""
        row = await self.get(message_id)
        if row is None:
            return None
        row.status = "FAILED"
        row.last_error = error[:_MAX_ERROR_LENGTH]
        if available_at is not None:
            row.available_at = available_at
        await _flush(self._session)
        return row
