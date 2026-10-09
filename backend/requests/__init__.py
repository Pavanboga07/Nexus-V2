"""Nexus Request Service (Component 5): remote-request lifecycle.

Creation, state transitions, approvals, and response correlation over
durable PostgreSQL state. Signs with Component 2, persists with
Component 4, queues dispatch through the transactional outbox. No
network, no agents, no models, no tools — dispatch and execution
belong to later components.
"""

from .errors import (
    ApprovalError,
    InactiveAgentError,
    InvalidStateTransitionError,
    PeerError,
    RequestConflictError,
    RequestServiceError,
    RequestValidationError,
    ResponseCorrelationError,
    RetryableInfrastructureError,
    UnknownAgentError,
    UnknownRequestError,
)
from .message_builder import build_remote_query_message
from .models import RequestRecord
from .ports import IdentityProvider
from .service import MAX_CAPABILITIES, MAX_QUERY_LENGTH, RequestService
from .states import (
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
    WAITING_APPROVAL,
    TERMINAL,
    TRANSITIONS,
    allowed_next,
    is_terminal,
    require_transition,
)

__all__ = [
    "RequestServiceError",
    "RequestValidationError",
    "UnknownAgentError",
    "InactiveAgentError",
    "UnknownRequestError",
    "PeerError",
    "InvalidStateTransitionError",
    "RequestConflictError",
    "ResponseCorrelationError",
    "RetryableInfrastructureError",
    "ApprovalError",
    "build_remote_query_message",
    "RequestRecord",
    "IdentityProvider",
    "RequestService",
    "MAX_QUERY_LENGTH",
    "MAX_CAPABILITIES",
    "CREATED",
    "QUEUED",
    "SENT",
    "DELIVERED",
    "WAITING_APPROVAL",
    "APPROVED",
    "PROCESSING",
    "COMPLETED",
    "REJECTED",
    "EXPIRED",
    "FAILED",
    "CANCELLED",
    "TERMINAL",
    "TRANSITIONS",
    "allowed_next",
    "is_terminal",
    "require_transition",
]
