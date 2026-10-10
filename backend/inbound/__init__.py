"""Component 9: secure, durable inbound REMOTE_QUERY registration."""

from .dispatcher import (
    HANDLED,
    REJECTED,
    UNHANDLED,
    DispatchResult,
    DispatcherAlreadyRunning,
    InboundDispatcher,
)
from .errors import (
    HandlerAlreadyRegistered,
    InboundConflictingDuplicate,
    InboundError,
    InboundExpired,
    InboundInactiveSender,
    InboundInfrastructureError,
    InboundInvalidEnvelope,
    InboundInvalidPayload,
    InboundInvalidSignature,
    InboundPeerError,
    InboundSenderKeyError,
    InboundUnknownSender,
    InboundUnsupportedType,
    InboundWrongRecipient,
)
from .models import DUPLICATE, REGISTERED, InboundOutcome
from .processor import (
    EVENT_REGISTERED,
    InboundProcessor,
)

__all__ = [
    "DUPLICATE",
    "EVENT_REGISTERED",
    "HANDLED",
    "REGISTERED",
    "REJECTED",
    "UNHANDLED",
    "DispatchResult",
    "DispatcherAlreadyRunning",
    "HandlerAlreadyRegistered",
    "InboundConflictingDuplicate",
    "InboundDispatcher",
    "InboundError",
    "InboundExpired",
    "InboundInactiveSender",
    "InboundInfrastructureError",
    "InboundInvalidEnvelope",
    "InboundInvalidPayload",
    "InboundInvalidSignature",
    "InboundPeerError",
    "InboundProcessor",
    "InboundOutcome",
    "InboundSenderKeyError",
    "InboundUnknownSender",
    "InboundUnsupportedType",
    "InboundWrongRecipient",
]
