"""Component 11 error model (receiver misuse, never internals).

Messages carry request/message ids and state names only. Response
payload content, signatures, keys, and exception text never appear:
rejected responses report a typed outcome, never the reason's detail.
"""

from __future__ import annotations

from backend.requests.errors import RequestServiceError


class ResponseReceiverError(RequestServiceError):
    """The response receiver was misused or misconfigured."""
