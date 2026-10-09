"""Request Service error model (application failures, never internals).

Messages carry request/message ids and state names only. Database
details stay chained (never rendered), and no private key, query
content, or secret material is ever included.
"""

from __future__ import annotations


class RequestServiceError(Exception):
    """Base class for Request Service failures."""


class RequestValidationError(RequestServiceError):
    """Caller input is unusable (bad query, capabilities, expiry, ids)."""


class UnknownAgentError(RequestServiceError):
    """A referenced agent has no local agent row."""


class InactiveAgentError(RequestServiceError):
    """The sending agent row exists but is deactivated."""


class UnknownRequestError(RequestServiceError):
    """No durable request matches the id."""


class PeerError(RequestServiceError):
    """No usable logical peer relationship for this direction."""


class InvalidStateTransitionError(RequestServiceError):
    """The requested lifecycle move is illegal from the current state."""


class RequestConflictError(RequestServiceError):
    """A duplicate request/message/outbox identity already exists."""


class ResponseCorrelationError(RequestServiceError):
    """A response cannot be attached to the referenced request."""


class ApprovalError(RequestServiceError):
    """An approval decision cannot be recorded as requested."""


class RetryableInfrastructureError(RequestServiceError):
    """A transient infrastructure failure (safe for a worker to retry).

    Raised only for connectivity-level problems (lost connections,
    refused connections, pool timeouts). Domain failures — conflicts,
    illegal transitions, validation, correlation — are never wrapped
    in this type. The original exception is always chained, while the
    public message carries no URLs, credentials, or internals.
    """
