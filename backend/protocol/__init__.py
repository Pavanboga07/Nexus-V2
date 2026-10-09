"""Nexus Protocol (Component 1): versioned message contracts.

Typed envelope, per-type payloads, strict validation, deterministic
serialization, and deterministic canonicalization for future (Component 2)
Ed25519 signing. No transport, database, crypto, LangGraph/LangChain, or
MCP dependencies.
"""

from .canonicalization import canonicalize_message, message_to_canonical_dict
from .models import (
    ApprovalRequestPayload,
    ApprovalResponsePayload,
    ConnectRequestPayload,
    ConnectResponsePayload,
    ErrorPayload,
    NexusMessage,
    Payload,
    RemoteQueryPayload,
    RemoteResponsePayload,
    payload_type_for,
)
from .serialization import (
    ENVELOPE_FIELDS,
    ProtocolSerializationError,
    format_timestamp,
    message_from_dict,
    message_from_json,
    message_to_dict,
    message_to_json,
    parse_timestamp,
    payload_from_dict,
    payload_to_dict,
)
from .types import (
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    MessageType,
    generate_message_id,
    generate_request_id,
    is_valid_agent_id,
    is_valid_capability,
    is_valid_message_id,
    is_valid_request_id,
)
from .validation import (
    ProtocolValidationError,
    assert_valid_message,
    is_expired,
    validate_message,
    validate_payload,
    validate_payload_dict,
)

__all__ = [
    "PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "MessageType",
    "NexusMessage",
    "Payload",
    "ConnectRequestPayload",
    "ConnectResponsePayload",
    "RemoteQueryPayload",
    "ApprovalRequestPayload",
    "ApprovalResponsePayload",
    "RemoteResponsePayload",
    "ErrorPayload",
    "payload_type_for",
    "ENVELOPE_FIELDS",
    "ProtocolValidationError",
    "validate_message",
    "assert_valid_message",
    "validate_payload",
    "validate_payload_dict",
    "is_expired",
    "ProtocolSerializationError",
    "message_to_dict",
    "message_from_dict",
    "message_to_json",
    "message_from_json",
    "payload_to_dict",
    "payload_from_dict",
    "parse_timestamp",
    "format_timestamp",
    "canonicalize_message",
    "message_to_canonical_dict",
    "generate_message_id",
    "generate_request_id",
    "is_valid_message_id",
    "is_valid_request_id",
    "is_valid_agent_id",
    "is_valid_capability",
]
