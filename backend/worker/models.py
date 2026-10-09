"""Worker data models and the bounded backoff policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class DispatchOutcome(str, Enum):
    """What one dispatch attempt established (evidence-graded)."""

    SENT = "SENT"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class DispatchResult:
    """Per-message outcome of a dispatcher batch."""

    message_id: str
    request_id: str
    outcome: DispatchOutcome
    detail: str = ""
    attempts: int = 0
    next_available_at: datetime | None = None


def compute_backoff(
    attempt: int, base_s: float, max_s: float, jitter: float
) -> float:
    """Bounded exponential backoff with injected jitter.

    ``attempt`` is 1-based, ``jitter`` is drawn from [0, 1) by the
    caller's randomness source (inject a constant in tests). Result is
    always within [base/2, max].
    """
    grown = base_s * (2.0 ** max(0, attempt - 1))
    capped = min(grown, max_s)
    clamped = min(1.0, max(0.0, jitter))
    return min(max_s, max(base_s / 2.0, capped * (0.5 + 0.5 * clamped)))
