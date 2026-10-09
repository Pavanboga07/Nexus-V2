"""Originating-side response outcome (typed result, not an exception)."""

from __future__ import annotations

from dataclasses import dataclass

from backend.requests.models import RequestRecord

#: The response was accepted; the request rests COMPLETED or FAILED.
REGISTERED = "REGISTERED"
#: An already-processed response was redelivered; nothing changed.
DUPLICATE = "DUPLICATE"
#: The response was invalid; nothing was recorded.
REJECTED = "REJECTED"


@dataclass(frozen=True)
class ResponseOutcome:
    """Point-in-time result of one response handling.

    ``detail`` carries the resulting request status (REGISTERED), the
    constant ``already-processed`` (DUPLICATE), or the rejecting error
    type name (REJECTED) — never payload content or exception text.
    ``record`` is the durable request view when one exists.
    """

    status: str
    request_id: str
    response_message_id: str | None
    detail: str = ""
    record: RequestRecord | None = None
