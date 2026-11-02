"""Operator recovery for stranded remote executions.

An execution lease makes a dead or partitioned claim visible instead of
leaving ``PROCESSING`` forever. Recovery is deliberately manual:

- ``retry`` moves ``RECOVERABLE -> APPROVED`` so the normal executor
  re-verifies approval, peer trust, expiry, and signing identity before
  running again. The caller must accept that side effects may have
  happened in the abandoned attempt.
- ``resolve`` moves ``RECOVERABLE -> FAILED`` without running anything.

There is no automatic retry here. A live lease is never stolen, and a
legacy ``PROCESSING`` row with no lease is never guessed at.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.persistence.database import session_scope
from backend.persistence.repositories.audit_events import AuditEventRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.requests.errors import (
    ApprovalError,
    InvalidStateTransitionError,
    RequestValidationError,
    UnknownRequestError,
)
from backend.requests.models import RequestRecord
from backend.requests.states import (
    APPROVED,
    FAILED,
    PROCESSING,
    RECOVERABLE,
    require_transition,
)

from .lease import CODE_LEASE_EXPIRED, CODE_OPERATOR_RESOLVED, can_recover


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionRecoveryService:
    """Resolve or explicitly retry one expired execution claim."""

    def __init__(self, sessions) -> None:
        self._sessions = sessions

    async def recover(
        self,
        request_id: str,
        actor_agent_id: str,
        *,
        retry: bool,
        now: datetime | None = None,
    ) -> RequestRecord:
        """Apply one operator decision to a recoverable request.

        Raises typed request errors; the API translates them to stable
        status codes. The caller is responsible for authenticating the
        operator; this service enforces that the actor is the persisted
        recipient.
        """
        current = now or _utcnow()
        if current.tzinfo is None or current.utcoffset() is None:
            raise RequestValidationError("now must be timezone-aware")
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            row = await repo.get_for_update(request_id)
            if row is None:
                raise UnknownRequestError(f"unknown request {request_id!r}")
            if row.recipient_agent_id != actor_agent_id:
                raise ApprovalError(
                    f"only recipient {row.recipient_agent_id!r} may "
                    "recover an execution"
                )
            if row.status == PROCESSING:
                if not can_recover(row, current):
                    raise InvalidStateTransitionError(
                        f"request {request_id!r} still has a live "
                        "execution lease"
                    )
                row = await repo.update(
                    request_id,
                    status=RECOVERABLE,
                    execution_lease_owner=None,
                    execution_lease_token=None,
                    execution_lease_expires_at=None,
                    execution_heartbeat_at=None,
                    failure_code=CODE_LEASE_EXPIRED,
                    failure_message=(
                        "execution lease expired; operator must retry "
                        "or resolve"
                    ),
                )
                assert row is not None
                await AuditEventRepository(session).create(
                    "EXECUTION_LEASE_EXPIRED",
                    actor_agent_id=actor_agent_id,
                    request_id=request_id,
                    meta={"attempt": row.execution_attempt},
                )
            elif row.status != RECOVERABLE:
                raise InvalidStateTransitionError(
                    f"request {request_id!r} in {row.status!r} is not "
                    "recoverable"
                )

            target = APPROVED if retry else FAILED
            require_transition(RECOVERABLE, target)
            fields = {
                "status": target,
                "execution_lease_owner": None,
                "execution_lease_token": None,
                "execution_lease_expires_at": None,
                "execution_heartbeat_at": None,
                "failure_code": None if retry else CODE_OPERATOR_RESOLVED,
                "failure_message": (
                    None
                    if retry
                    else "execution resolved as failed by the operator"
                ),
                "completed_at": None if retry else current,
            }
            updated = await repo.update(request_id, **fields)
            assert updated is not None
            await AuditEventRepository(session).create(
                (
                    "EXECUTION_RECOVERY_RETRY"
                    if retry
                    else "EXECUTION_RECOVERY_RESOLVED"
                ),
                actor_agent_id=actor_agent_id,
                request_id=request_id,
                meta={
                    "attempt": row.execution_attempt,
                    "target": target,
                },
            )
            return RequestRecord.from_row(updated)
