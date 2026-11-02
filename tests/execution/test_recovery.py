"""Operator recovery and lease fencing (isolated real PostgreSQL)."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.execution import (
    COMPLETED,
    ExecutionConflictError,
    ExecutionRecoveryService,
)
from backend.execution.lease import CODE_OPERATOR_RESOLVED
from backend.persistence.database import session_scope
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.requests.errors import (
    ApprovalError,
    InvalidStateTransitionError,
)
from backend.requests.states import APPROVED, FAILED, PROCESSING

from .conftest import (
    AnswerModel,
    Keys,
    make_executor,
    make_runtime,
    seed_approved,
)


async def _strand(db, request_id, *, now, token="lease-dead", attempt=1):
    """Put a request into PROCESSING with an expired lease."""
    async with session_scope(db.sessions) as session:
        await RemoteRequestRepository(session).update(
            request_id,
            status=PROCESSING,
            execution_lease_owner="executor-dead",
            execution_lease_token=token,
            execution_lease_expires_at=now - timedelta(seconds=1),
            execution_heartbeat_at=now - timedelta(seconds=10),
            execution_attempt=attempt,
        )


@pytest.mark.asyncio
async def test_operator_resolve_terminates_expired_execution(db):
    keys = Keys()
    _sender, recipient, request_id = await seed_approved(db, keys)
    now = datetime.now(timezone.utc)
    await _strand(db, request_id, now=now)

    record = await ExecutionRecoveryService(db.sessions).recover(
        request_id, recipient.agent_id, retry=False
    )

    assert record.status == FAILED
    assert record.failure_code == CODE_OPERATOR_RESOLVED
    assert record.completed_at is not None
    async with session_scope(db.sessions) as session:
        row = await RemoteRequestRepository(session).get(request_id)
        assert row is not None
        assert row.execution_lease_token is None
        assert row.execution_lease_expires_at is None


@pytest.mark.asyncio
async def test_operator_retry_goes_through_normal_execution(db):
    keys = Keys()
    _sender, recipient, request_id = await seed_approved(db, keys)
    now = datetime.now(timezone.utc)
    await _strand(db, request_id, now=now)

    recovered = await ExecutionRecoveryService(db.sessions).recover(
        request_id, recipient.agent_id, retry=True
    )
    assert recovered.status == APPROVED
    assert recovered.failure_code is None

    outcome = await make_executor(
        db, keys, recipient.agent_id, make_runtime(AnswerModel())
    ).execute(request_id)
    assert outcome.status == COMPLETED
    assert outcome.executed is True


@pytest.mark.asyncio
async def test_live_lease_cannot_be_stolen(db):
    keys = Keys()
    _sender, recipient, request_id = await seed_approved(db, keys)
    async with session_scope(db.sessions) as session:
        await RemoteRequestRepository(session).update(
            request_id,
            status=PROCESSING,
            execution_lease_owner="executor-live",
            execution_lease_token="lease-live",
            execution_lease_expires_at=(
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ),
            execution_attempt=1,
        )

    with pytest.raises(InvalidStateTransitionError):
        await ExecutionRecoveryService(db.sessions).recover(
            request_id, recipient.agent_id, retry=True
        )


@pytest.mark.asyncio
async def test_only_the_persisted_recipient_can_recover(db):
    keys = Keys()
    _sender, _recipient, request_id = await seed_approved(db, keys)
    await _strand(db, request_id, now=datetime.now(timezone.utc))

    with pytest.raises(ApprovalError):
        await ExecutionRecoveryService(db.sessions).recover(
            request_id, "agent_someone_else", retry=False
        )


@pytest.mark.asyncio
async def test_stale_worker_cannot_finalize_after_recovery(db):
    keys = Keys()
    sender, recipient, request_id = await seed_approved(db, keys)
    now = datetime.now(timezone.utc)
    await _strand(db, request_id, now=now, token="lease-old")
    await ExecutionRecoveryService(db.sessions).recover(
        request_id, recipient.agent_id, retry=True
    )
    # A new attempt claims the retried request with a fresh token.
    async with session_scope(db.sessions) as session:
        await RemoteRequestRepository(session).update(
            request_id,
            status=PROCESSING,
            execution_lease_owner="executor-new",
            execution_lease_token="lease-new",
            execution_lease_expires_at=(
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ),
            execution_attempt=2,
        )

    executor = make_executor(
        db, keys, recipient.agent_id, make_runtime(AnswerModel())
    )
    async with session_scope(db.sessions) as session:
        row = await RemoteRequestRepository(session).get(request_id)
        assert row is not None
        with pytest.raises(ExecutionConflictError):
            await executor._finalize(
                session,
                request_id,
                sender.agent_id,
                row.expires_at,
                "stale answer",
                None,
                lease_token="lease-old",
            )
