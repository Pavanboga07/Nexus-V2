"""Protocol message construction for remote requests (no network, no keys).

Builds the Component 1 REMOTE_QUERY envelope that represents a request
and structurally validates it (signature presence exempted: signing
happens in the service via Component 2). The natural-language query is
transported, never interpreted.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import validate_message

from .errors import RequestValidationError


def build_remote_query_message(
    *,
    sender_agent_id: str,
    recipient_agent_id: str,
    request_id: str,
    message_id: str,
    query: str,
    requested_capabilities: list[Any],
    created_at: datetime,
    expires_at: datetime,
) -> NexusMessage:
    """Build an unsigned REMOTE_QUERY, structurally validated.

    Raises :class:`RequestValidationError` for any structural problem;
    the missing signature is expected (the service signs next).
    """
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id,
        request_id=request_id,
        sender=sender_agent_id,
        recipient=recipient_agent_id,
        type=MessageType.REMOTE_QUERY,
        created_at=created_at,
        expires_at=expires_at,
        payload=RemoteQueryPayload(
            query=query,
            requested_capabilities=list(requested_capabilities),
        ),
        signature="",
    )
    candidate = dataclasses.replace(message, signature="nexus-presigning")
    errors = validate_message(candidate, check_expiry=False)
    if errors:
        raise RequestValidationError(
            f"invalid remote query message: {'; '.join(errors)}"
        )
    return message
