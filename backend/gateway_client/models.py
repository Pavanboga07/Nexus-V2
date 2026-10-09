"""Gateway client data models (transport state only)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConnectionState(str, Enum):
    """Lifecycle of one client-side Gateway connection."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class ClientConfig:
    """Client safety boundaries (documented, no hidden behavior)."""

    handshake_timeout_s: float = 10.0
    ack_timeout_s: float = 30.0
    max_inbound_queue: int = 100
    gateway_agent_id: str = "gateway"
    # Must exceed the server's max frame: legitimately large DELIVER
    # frames would otherwise trip the client's own receive limit.
    max_frame_size: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class AckResult:
    """One correlated Gateway acknowledgement (transport fact only)."""

    message_id: str
    status: str
    detail: str = ""
    request_id: str = ""
