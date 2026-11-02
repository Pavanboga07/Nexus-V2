"""Pure execution-lease policy tests (no database required)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.execution.lease import (
    LeaseConfig,
    can_recover,
    lease_expired,
    new_lease,
)
from backend.requests.states import PROCESSING, RECOVERABLE


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def test_new_lease_sets_owner_token_expiry_and_heartbeat():
    lease = new_lease("executor-a", NOW, LeaseConfig(ttl_s=60))
    assert lease.owner == "executor-a"
    assert len(lease.token) >= 16
    assert lease.heartbeat_at == NOW
    assert lease.expires_at == NOW + timedelta(seconds=60)


def test_lease_expiry_requires_a_lease():
    row = SimpleNamespace(
        status=PROCESSING,
        execution_lease_expires_at=None,
    )
    assert lease_expired(row, NOW) is False


def test_lease_expiry_is_inclusive_at_the_boundary():
    row = SimpleNamespace(
        status=PROCESSING,
        execution_lease_expires_at=NOW,
    )
    assert lease_expired(row, NOW) is True


def test_recovery_requires_processing_and_an_expired_lease():
    row = SimpleNamespace(
        status=PROCESSING,
        execution_lease_expires_at=NOW - timedelta(seconds=1),
    )
    assert can_recover(row, NOW) is True
    row.status = RECOVERABLE
    assert can_recover(row, NOW) is False
    row.status = PROCESSING
    row.execution_lease_expires_at = NOW + timedelta(seconds=1)
    assert can_recover(row, NOW) is False
