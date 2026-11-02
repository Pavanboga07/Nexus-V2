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
    MessageTooLargeError,
    NotConnectedError,
)
from .models import AckResult, ClientConfig, ConnectionState
from .supervisor import GatewaySupervisor, SupervisorConfig

__all__ = [
    "GatewayClient",
    "GatewaySupervisor",
    "SupervisorConfig",
    "AckResult",
    "ClientConfig",
    "ConnectionState",
    "GatewayClientError",
    "HandshakeFailedError",
    "NotConnectedError",
    "AckTimeoutError",
    "ConnectionLostError",
    "MessageTooLargeError",
]
