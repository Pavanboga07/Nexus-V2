"""Nexus Protocol typed message models.

The envelope (:class:`NexusMessage`) is the primary message representation;
an unstructured dict must never be used in its place. Payload dataclasses
define the contract per :class:`MessageType`; they carry data only and
perform no validation, transport, or business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Union

from .types import MessageType


@dataclass
class ConnectRequestPayload:
    """Payload for CONNECT_REQUEST (connection data structure only)."""

    agent_id: str
    display_name: Optional[str] = None
    context: Optional[str] = None


@dataclass
class ConnectResponsePayload:
    """Payload for CONNECT_RESPONSE (connection decision only)."""

    accepted: bool
    responder_agent_id: str
    reason: Optional[str] = None


@dataclass
class RemoteQueryPayload:
    """Payload for REMOTE_QUERY. Transports the query; never interprets it."""

    query: str
    requested_capabilities: list[str] = field(default_factory=list)


@dataclass
class ApprovalRequestPayload:
    """Payload for APPROVAL_REQUEST (structured info for User B)."""

    request_ref: str
    query: str
    requested_capabilities: list[str] = field(default_factory=list)
    context: Optional[str] = None


@dataclass
class ApprovalResponsePayload:
    """Payload for APPROVAL_RESPONSE (approval decision only)."""

    approved: bool
    reason: Optional[str] = None


@dataclass
class RemoteResponsePayload:
    """Payload for REMOTE_RESPONSE (agent answer + optional extras)."""

    status: str  # "ok" | "error"
    answer: str
    structured_data: Optional[dict[str, Any]] = None
    citations: Optional[list[str]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class ErrorPayload:
    """Payload for ERROR. Plain strings only: no stack traces, paths,
    secrets, or internal database details may be placed here."""

    error_code: str
    message: str
    retryable: bool


Payload = Union[
    ConnectRequestPayload,
    ConnectResponsePayload,
    RemoteQueryPayload,
    ApprovalRequestPayload,
    ApprovalResponsePayload,
    RemoteResponsePayload,
    ErrorPayload,
]


@dataclass
class NexusMessage:
    """Common Nexus message envelope.

    Field names match the protocol wire format exactly. ``message_id``
    identifies one physical protocol message; ``request_id`` identifies one
    logical remote operation (many messages may share one ``request_id``).
    ``signature`` is an opaque string field reserved for Component 2
    (Ed25519); this component never generates or verifies it.
    """

    protocol_version: str
    message_id: str
    request_id: str
    sender: str
    recipient: str
    type: MessageType
    created_at: datetime
    expires_at: datetime
    payload: Payload
    signature: str


#: Maps each message type to its expected typed payload class.
PAYLOAD_TYPE_BY_MESSAGE: dict[MessageType, type] = {
    MessageType.CONNECT_REQUEST: ConnectRequestPayload,
    MessageType.CONNECT_RESPONSE: ConnectResponsePayload,
    MessageType.REMOTE_QUERY: RemoteQueryPayload,
    MessageType.APPROVAL_REQUEST: ApprovalRequestPayload,
    MessageType.APPROVAL_RESPONSE: ApprovalResponsePayload,
    MessageType.REMOTE_RESPONSE: RemoteResponsePayload,
    MessageType.ERROR: ErrorPayload,
}


def payload_type_for(message_type: MessageType) -> type:
    """Return the payload dataclass expected for ``message_type``."""
    return PAYLOAD_TYPE_BY_MESSAGE[message_type]
