"""Inbound registration outcome (typed result, not an exception)."""

from __future__ import annotations

from dataclasses import dataclass

from backend.requests.models import RequestRecord

#: A valid REMOTE_QUERY was durably registered for approval.
REGISTERED = "REGISTERED"
#: The message repeats an already-registered request; nothing new stored.
DUPLICATE = "DUPLICATE"


@dataclass(frozen=True)
class InboundOutcome:
    """Point-in-time result of processing one inbound message."""

    status: str
    request_id: str
    message_id: str
    record: RequestRecord
