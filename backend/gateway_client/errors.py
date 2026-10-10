"""Gateway client error model (transport failures, never secrets)."""

from __future__ import annotations


class GatewayClientError(Exception):
    """Base class for client-side transport failures."""


class HandshakeFailedError(GatewayClientError):
    """Authentication with the Gateway was rejected or timed out."""


class NotConnectedError(GatewayClientError):
    """An operation needs an established connection first."""


class AckTimeoutError(GatewayClientError):
    """No acknowledgement arrived in time (outcome ambiguous)."""


class ConnectionLostError(GatewayClientError):
    """The connection dropped mid-operation (outcome ambiguous)."""


class MessageTooLargeError(GatewayClientError):
    """The envelope exceeds the sender-side size limit (never sent).

    Raised deterministically before any bytes move, so callers must
    classify it as a permanent pre-send rejection — never as an
    ambiguous transmission.
    """
