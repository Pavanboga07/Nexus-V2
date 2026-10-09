"""Remote-execution outcome (typed result, not an exception)."""

from __future__ import annotations

from dataclasses import dataclass

from backend.requests.models import RequestRecord

#: The agent ran and a success response was durably queued.
COMPLETED = "COMPLETED"
#: Execution failed safely; an error response was durably queued, or a
#: prior failure is reported without re-executing.
FAILED = "FAILED"
#: Another attempt owns this request; nothing ran here.
IN_PROGRESS = "IN_PROGRESS"


@dataclass(frozen=True)
class ExecutionOutcome:
    """Point-in-time result of one execution call.

    ``executed`` tells whether this call invoked the Agent Runtime:
    False means the outcome replays already-durable state (duplicate
    call, prior completion/failure, or a live claim elsewhere).
    """

    status: str
    request_id: str
    response_message_id: str | None
    executed: bool
    record: RequestRecord
