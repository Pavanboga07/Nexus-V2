"""Nexus Gateway client (Component 6): authenticated transport, no logic.

One :class:`GatewayClient` per local agent identity; connection pooling
across identities (if needed) belongs to the worker layer.
"""

from .client import GatewayClient
from .errors import (
    AckTimeoutError,
    ConnectionLostError,
    GatewayClientError,
    HandshakeFailedError,
    NotConnectedError,
)
from .models import AckResult, ClientConfig, ConnectionState

__all__ = [
    "GatewayClient",
    "AckResult",
    "ClientConfig",
    "ConnectionState",
    "GatewayClientError",
    "HandshakeFailedError",
    "NotConnectedError",
    "AckTimeoutError",
    "ConnectionLostError",
]
