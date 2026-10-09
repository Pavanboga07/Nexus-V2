"""Nexus Protocol serialization (typed message <-> JSON).

Strict and transport-independent: stdlib ``json`` only. Invalid data is
rejected with :class:`ProtocolSerializationError`; values are never
silently coerced into valid ones.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .models import (
    ApprovalRequestPayload,
    ApprovalResponsePayload,
    ConnectRequestPayload,
    ConnectResponsePayload,
    ErrorPayload,
    NexusMessage,
    RemoteQueryPayload,
    RemoteResponsePayload,
    payload_type_for,
)
from .types import (
    SUPPORTED_PROTOCOL_VERSIONS,
    MessageType,
    is_valid_agent_id,
    is_valid_message_id,
    is_valid_request_id,
)
from .validation import validate_payload_dict


class ProtocolSerializationError(ValueError):
    """Raised when wire data cannot be parsed into a valid message."""

    def __init__(self, errors: list[str]) -> None:
        self.errors: list[str] = list(errors)
        super().__init__("; ".join(self.errors))


#: Envelope fields in protocol order. Wire objects must contain exactly
#: these fields: missing or unknown fields are rejected.
ENVELOPE_FIELDS: tuple[str, ...] = (
    "protocol_version",
    "message_id",
    "request_id",
    "sender",
    "recipient",
    "type",
    "created_at",
    "expires_at",
    "payload",
    "signature",
)


def format_timestamp(value: datetime) -> str:
    """Format a timezone-aware datetime as canonical ISO-8601 UTC text."""
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProtocolSerializationError(
            ["timestamp must be a timezone-aware datetime"]
        )
    return value.astimezone(timezone.utc).isoformat()


def parse_timestamp(value: Any) -> datetime:
    """Parse ISO-8601 text into a timezone-aware datetime.

    Naive datetimes (no offset) and non-string values are rejected.
    """
    if not isinstance(value, str) or not value:
        raise ProtocolSerializationError(
            [f"invalid timestamp: {value!r}"]
        )
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ProtocolSerializationError(
            [f"invalid timestamp: {value!r}"]
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolSerializationError(
            [f"invalid timestamp (timezone required): {value!r}"]
        )
    return parsed


def payload_to_dict(payload: Any) -> dict[str, Any]:
    """Convert a typed payload to a plain dict (``None`` optionals omitted)."""
    if isinstance(payload, ConnectRequestPayload):
        data: dict[str, Any] = {"agent_id": payload.agent_id}
        if payload.display_name is not None:
            data["display_name"] = payload.display_name
        if payload.context is not None:
            data["context"] = payload.context
        return data
    if isinstance(payload, ConnectResponsePayload):
        data = {
            "accepted": payload.accepted,
            "responder_agent_id": payload.responder_agent_id,
        }
        if payload.reason is not None:
            data["reason"] = payload.reason
        return data
    if isinstance(payload, RemoteQueryPayload):
        return {
            "query": payload.query,
            "requested_capabilities": list(payload.requested_capabilities),
        }
    if isinstance(payload, ApprovalRequestPayload):
        data = {
            "request_ref": payload.request_ref,
            "query": payload.query,
            "requested_capabilities": list(payload.requested_capabilities),
        }
        if payload.context is not None:
            data["context"] = payload.context
        return data
    if isinstance(payload, ApprovalResponsePayload):
        data = {"approved": payload.approved}
        if payload.reason is not None:
            data["reason"] = payload.reason
        return data
    if isinstance(payload, RemoteResponsePayload):
        data = {"status": payload.status, "answer": payload.answer}
        if payload.structured_data is not None:
            data["structured_data"] = payload.structured_data
        if payload.citations is not None:
            data["citations"] = list(payload.citations)
        if payload.error_code is not None:
            data["error_code"] = payload.error_code
        if payload.error_message is not None:
            data["error_message"] = payload.error_message
        return data
    if isinstance(payload, ErrorPayload):
        return {
            "error_code": payload.error_code,
            "message": payload.message,
            "retryable": payload.retryable,
        }
    raise ProtocolSerializationError(
        [f"unknown payload type: {type(payload).__name__}"]
    )


def payload_from_dict(
    message_type: MessageType, payload: Any
) -> Any:
    """Build a typed payload from a raw dict (strict, no coercion)."""
    errors = validate_payload_dict(message_type, payload)
    if errors:
        raise ProtocolSerializationError(errors)
    assert isinstance(payload, dict)
    if message_type is MessageType.CONNECT_REQUEST:
        return ConnectRequestPayload(
            agent_id=payload["agent_id"],
            display_name=payload.get("display_name"),
            context=payload.get("context"),
        )
    if message_type is MessageType.CONNECT_RESPONSE:
        return ConnectResponsePayload(
            accepted=payload["accepted"],
            responder_agent_id=payload["responder_agent_id"],
            reason=payload.get("reason"),
        )
    if message_type is MessageType.REMOTE_QUERY:
        return RemoteQueryPayload(
            query=payload["query"],
            requested_capabilities=list(payload["requested_capabilities"]),
        )
    if message_type is MessageType.APPROVAL_REQUEST:
        return ApprovalRequestPayload(
            request_ref=payload["request_ref"],
            query=payload["query"],
            requested_capabilities=list(payload["requested_capabilities"]),
            context=payload.get("context"),
        )
    if message_type is MessageType.APPROVAL_RESPONSE:
        return ApprovalResponsePayload(
            approved=payload["approved"],
            reason=payload.get("reason"),
        )
    if message_type is MessageType.REMOTE_RESPONSE:
        citations = payload.get("citations")
        return RemoteResponsePayload(
            status=payload["status"],
            answer=payload["answer"],
            structured_data=payload.get("structured_data"),
            citations=list(citations) if citations is not None else None,
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )
    if message_type is MessageType.ERROR:
        return ErrorPayload(
            error_code=payload["error_code"],
            message=payload["message"],
            retryable=payload["retryable"],
        )
    raise ProtocolSerializationError(
        [f"invalid message type: {message_type!r}"]
    )


def message_to_dict(message: NexusMessage) -> dict[str, Any]:
    """Convert a typed message to a plain dict (protocol field order).

    Validate-before-emit: the message type must be a valid
    :class:`MessageType` and the payload must match that type, otherwise
    no wire representation is produced. Signature presence and clock
    expiry are deliberately NOT checked here: canonicalization runs
    pre-signing (Component 2 signs these bytes, then attaches the
    signature), so an unsigned message must still emit.
    """
    if not isinstance(message, NexusMessage):
        raise ProtocolSerializationError(["message must be a NexusMessage"])
    if not isinstance(message.type, MessageType):
        raise ProtocolSerializationError(
            [f"invalid message type: {message.type!r}"]
        )
    try:
        expected = payload_type_for(message.type)
    except KeyError:
        raise ProtocolSerializationError(
            [f"invalid message type: {message.type!r}"]
        ) from None
    if not isinstance(message.payload, expected):
        raise ProtocolSerializationError(
            [
                f"payload must be {expected.__name__} "
                f"for type {message.type.value}"
            ]
        )
    return {
        "protocol_version": message.protocol_version,
        "message_id": message.message_id,
        "request_id": message.request_id,
        "sender": message.sender,
        "recipient": message.recipient,
        "type": message.type.value
        if isinstance(message.type, MessageType)
        else message.type,
        "created_at": format_timestamp(message.created_at),
        "expires_at": format_timestamp(message.expires_at),
        "payload": payload_to_dict(message.payload),
        "signature": message.signature,
    }


def message_from_dict(data: Any) -> NexusMessage:
    """Build a typed message from a raw dict (strict, no coercion).

    Structural ordering (``expires_at`` after ``created_at``) is enforced
    here; liveness (expiry against the current clock) is a validation-time
    concern, so already-stored messages still parse.
    """
    if not isinstance(data, dict):
        raise ProtocolSerializationError(["message must be a JSON object"])
    errors: list[str] = []
    for name in sorted(set(data) - set(ENVELOPE_FIELDS)):
        errors.append(f"unknown envelope field: {name!r}")
    for name in ENVELOPE_FIELDS:
        if name not in data:
            errors.append(f"missing envelope field: {name!r}")
    if errors:
        raise ProtocolSerializationError(errors)

    if data["protocol_version"] not in SUPPORTED_PROTOCOL_VERSIONS:
        errors.append(
            "unsupported protocol_version: "
            f"{data['protocol_version']!r}"
        )
    try:
        message_type = MessageType(data["type"])
    except ValueError:
        errors.append(f"invalid message type: {data['type']!r}")
        message_type = None  # type: ignore[assignment]

    if not is_valid_message_id(data["message_id"]):
        errors.append(f"invalid message_id: {data['message_id']!r}")
    if not is_valid_request_id(data["request_id"]):
        errors.append(f"invalid request_id: {data['request_id']!r}")
    if not is_valid_agent_id(data["sender"]):
        errors.append(f"invalid sender: {data['sender']!r}")
    if not is_valid_agent_id(data["recipient"]):
        errors.append(f"invalid recipient: {data['recipient']!r}")

    try:
        created_at = parse_timestamp(data["created_at"])
    except ProtocolSerializationError as exc:
        errors.extend(exc.errors)
        created_at = None  # type: ignore[assignment]
    try:
        expires_at = parse_timestamp(data["expires_at"])
    except ProtocolSerializationError as exc:
        errors.extend(exc.errors)
        expires_at = None  # type: ignore[assignment]
    if created_at is not None and expires_at is not None:
        if expires_at <= created_at:
            errors.append("expires_at must be after created_at")

    payload: Any = None
    if message_type is not None:
        try:
            payload = payload_from_dict(message_type, data["payload"])
        except ProtocolSerializationError as exc:
            errors.extend(exc.errors)

    signature = data["signature"]
    if not isinstance(signature, str) or not signature:
        errors.append("signature must be a non-empty string")

    if errors:
        raise ProtocolSerializationError(errors)

    return NexusMessage(
        protocol_version=data["protocol_version"],
        message_id=data["message_id"],
        request_id=data["request_id"],
        sender=data["sender"],
        recipient=data["recipient"],
        type=message_type,
        created_at=created_at,
        expires_at=expires_at,
        payload=payload,
        signature=signature,
    )


def message_to_json(message: NexusMessage) -> str:
    """Serialize a typed message to deterministic JSON text."""
    data = message_to_dict(message)
    try:
        return json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolSerializationError(
            [f"message is not JSON serializable: {exc}"]
        ) from exc


def message_from_json(text: Any) -> NexusMessage:
    """Parse JSON text into a typed message (malformed JSON rejected)."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolSerializationError(
                ["message must be valid UTF-8 JSON"]
            ) from None
    if not isinstance(text, str):
        raise ProtocolSerializationError(["message must be JSON text"])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ProtocolSerializationError(["malformed JSON"]) from None
    return message_from_dict(data)
