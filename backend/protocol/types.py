"""Nexus Protocol core types.

Stdlib only. This module has no transport, database, cryptographic,
LangGraph/LangChain, or MCP dependencies by design.
"""

from __future__ import annotations

import re
import uuid
from enum import Enum

#: Current Nexus protocol version. Every message must carry this value
#: in its ``protocol_version`` field.
PROTOCOL_VERSION: str = "1.0"

#: Protocol versions accepted by this implementation. Only ``1.0`` is
#: supported; future-version compatibility is explicitly out of scope.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("1.0",)


class MessageType(str, Enum):
    """Strongly typed Nexus message types (Component 1 scope)."""

    CONNECT_REQUEST = "CONNECT_REQUEST"
    CONNECT_RESPONSE = "CONNECT_RESPONSE"
    REMOTE_QUERY = "REMOTE_QUERY"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    APPROVAL_RESPONSE = "APPROVAL_RESPONSE"
    REMOTE_RESPONSE = "REMOTE_RESPONSE"
    ERROR = "ERROR"


# Identifiers: 1-128 chars, must start with an alphanumeric character and
# may then contain alphanumerics plus ``_ - . :``. This covers prefixed ids
# such as ``msg_...`` / ``req_...`` and agent ids such as ``agent:alice``.
# Validation is purely syntactic and deterministic (no database involved).
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.:]{0,127}$")

# Requested capabilities look like ``memory.read``: dot-separated segments
# of alphanumerics, ``_``, ``-``.
_CAPABILITY_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")


def is_valid_message_id(value: object) -> bool:
    """Return True iff ``value`` is a syntactically valid ``message_id``."""
    return isinstance(value, str) and _ID_RE.match(value) is not None


def is_valid_request_id(value: object) -> bool:
    """Return True iff ``value`` is a syntactically valid ``request_id``."""
    return isinstance(value, str) and _ID_RE.match(value) is not None


def is_valid_agent_id(value: object) -> bool:
    """Return True iff ``value`` is a syntactically valid agent id.

    Used for ``sender``, ``recipient`` and agent references in payloads.
    """
    return isinstance(value, str) and _ID_RE.match(value) is not None


def is_valid_capability(value: object) -> bool:
    """Return True iff ``value`` is a syntactically valid capability scope."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and _CAPABILITY_RE.match(value) is not None
    )


def generate_message_id(prefix: str = "msg_") -> str:
    """Generate a unique ``message_id`` without any database dependency."""
    return f"{prefix}{uuid.uuid4().hex}"


def generate_request_id(prefix: str = "req_") -> str:
    """Generate a unique ``request_id`` without any database dependency."""
    return f"{prefix}{uuid.uuid4().hex}"
