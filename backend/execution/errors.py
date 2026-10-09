"""Component 10 error model (execution failures, never internals).

Messages carry request/message ids and state names only. Agent
output, tool results, exception text, and key material are never
included: failure details sent to a requester use static safe codes.
"""

from __future__ import annotations

from backend.requests.errors import RequestServiceError


class ExecutionError(RequestServiceError):
    """Base class for remote-execution failures."""


class RequestNotApprovedError(ExecutionError):
    """The request is not in an executable approval state."""


class NotInboundRequestError(ExecutionError):
    """The request is not inbound work for the configured local agent."""


class ApprovalMissingError(ExecutionError):
    """No valid recipient approval decision is recorded."""


class RequestExpiredError(ExecutionError):
    """The request expired before (or during) execution."""


class ExecutionConflictError(ExecutionError):
    """The request left the expected state mid-execution; nothing was
    overwritten and no second response was recorded."""


class ExecutionInfrastructureError(ExecutionError):
    """A transient infrastructure failure (safe for a worker to retry).

    Raised only for connectivity-level problems. Domain failures are
    never wrapped in this type. The original exception is always
    chained, while the public message carries no internals.
    """
