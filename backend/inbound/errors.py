"""Component 9 error model (inbound registration failures, never internals).

Messages carry message/request ids and agent ids only. Database
details stay chained (never rendered); no private key, query content,
or secret material is ever included.
"""

from __future__ import annotations


class InboundError(Exception):
    """Base class for inbound processing failures."""


class InboundInvalidEnvelope(InboundError):
    """The message is structurally invalid or undecodable."""


class InboundUnsupportedType(InboundError):
    """The message type is not handled by the inbound processor."""


class InboundWrongRecipient(InboundError):
    """The message is not addressed to this agent."""


class InboundInvalidPayload(InboundError):
    """The request payload fails inbound policy (empty, oversized)."""


class InboundUnknownSender(InboundError):
    """The sender has no local agent row."""


class InboundInactiveSender(InboundError):
    """The sender row exists but is deactivated."""


class InboundSenderKeyError(InboundError):
    """The stored sender key is missing, undecodable, unsupported, or
    does not derive the claimed sender id."""


class InboundPeerError(InboundError):
    """No usable logical peer relationship for this direction."""


class InboundInvalidSignature(InboundError):
    """The envelope signature is undecodable or fails verification."""


class InboundExpired(InboundError):
    """The message arrived at or past its expiry."""


class InboundConflictingDuplicate(InboundError):
    """An id is reused with different content; nothing was overwritten."""


class InboundInfrastructureError(InboundError):
    """A transient infrastructure failure (safe for a worker to retry).

    Raised only for connectivity-level problems. Domain failures —
    conflicts, validation, verification — are never wrapped in this
    type. The original exception is always chained, while the public
    message carries no internals.
    """


class HandlerAlreadyRegistered(InboundError):
    """A response handler is already registered on this dispatcher."""
