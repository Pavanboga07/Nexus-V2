"""State-machine tests: legal moves pass, all else fails loudly (no DB)."""

import pytest

from backend.requests.errors import InvalidStateTransitionError
from backend.requests.states import (
    APPROVED,
    CANCELLED,
    COMPLETED,
    CREATED,
    DELIVERED,
    EXPIRED,
    FAILED,
    PROCESSING,
    QUEUED,
    REJECTED,
    SENT,
    TERMINAL,
    TRANSITIONS,
    WAITING_APPROVAL,
    allowed_next,
    is_terminal,
    require_transition,
)

HAPPY_PATH = [
    (CREATED, QUEUED),
    (QUEUED, SENT),
    (SENT, DELIVERED),
    (DELIVERED, WAITING_APPROVAL),
    (WAITING_APPROVAL, APPROVED),
    (APPROVED, PROCESSING),
    (PROCESSING, COMPLETED),
]


@pytest.mark.parametrize("current,target", HAPPY_PATH)
def test_every_legal_transition_succeeds(current, target):  # 1
    require_transition(current, target)
    assert target in allowed_next(current)


def test_illegal_transitions_fail():  # 2
    illegal = [
        (CREATED, SENT),
        (QUEUED, DELIVERED),
        (QUEUED, COMPLETED),
        (SENT, APPROVED),
        (DELIVERED, APPROVED),
        (WAITING_APPROVAL, PROCESSING),
        (WAITING_APPROVAL, COMPLETED),
        (APPROVED, "BOGUS_TARGET"),
        (PROCESSING, QUEUED),
        ("BOGUS", QUEUED),
    ]
    for current, target in illegal:
        with pytest.raises(InvalidStateTransitionError):
            require_transition(current, target)


@pytest.mark.parametrize(
    "terminal",
    [COMPLETED, REJECTED, EXPIRED, FAILED, CANCELLED],
)
@pytest.mark.parametrize(
    "target",
    [CREATED, QUEUED, SENT, DELIVERED, WAITING_APPROVAL, APPROVED,
     PROCESSING, COMPLETED],
)
def test_terminal_states_cannot_be_resurrected(terminal, target):  # 3
    assert is_terminal(terminal)
    assert allowed_next(terminal) == frozenset()
    with pytest.raises(InvalidStateTransitionError):
        require_transition(terminal, target)


def test_expiry_reachable_from_all_live_states():  # 5
    for state, targets in TRANSITIONS.items():
        if state not in TERMINAL:
            assert EXPIRED in targets, state


def test_cancellation_reachable_from_all_live_states():  # 6
    for state, targets in TRANSITIONS.items():
        if state not in TERMINAL:
            assert CANCELLED in targets, state


def test_terminal_set_complete():
    assert TERMINAL == frozenset(
        {COMPLETED, REJECTED, EXPIRED, FAILED, CANCELLED}
    )
