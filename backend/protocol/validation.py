"""Nexus Protocol structural validation.

Validates message shape only: version, type, ids, timestamps, payload
structure, signature-field presence. This module never makes security
authorization decisions (e.g. whether an agent *may* use a capability);
it only checks that declared values are well-formed.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Optional

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
    is_valid_capability,
    is_valid_message_id,
    is_valid_request_id,
)

_ERROR_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")

REMOTE_RESPONSE_STATUSES: tuple[str, ...] = ("ok", "error")


class ProtocolValidationError(ValueError):
    """Raised when a protocol message fails structural validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors: list[str] = list(errors)
        super().__init__("; ".join(self.errors))


def _is_tz_aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _require_aware_now(now: Any) -> datetime:
    """Return ``now`` if usable as a comparison clock, else raise."""
    if not _is_tz_aware(now):
        raise ProtocolValidationError(
            ["now must be a timezone-aware datetime"]
        )
    return now


def _check_json_value(value: Any, field: str, errors: list[str]) -> None:
    """Recursively reject values that cannot survive JSON serialization.

    Small focused helper for ``structured_data``: accepted values are
    None, bool, int, finite float, str (without lone surrogates), lists
    of accepted values, and dicts with string keys and accepted values.
    Everything else (objects, sets, tuples, bytes, NaN/Infinity, lone
    surrogates, non-string keys) is rejected so that a validated message
    is guaranteed serializable and canonicalizable.
    """
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            errors.append(
                f"{field} must not contain non-finite numbers"
            )
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            errors.append(f"{field} must not contain lone surrogates")
        return
    if isinstance(value, list):
        for item in value:
            _check_json_value(item, field, errors)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                errors.append(f"{field} must have string keys")
            elif any(0xD800 <= ord(char) <= 0xDFFF for char in key):
                errors.append(f"{field} must not contain lone surrogates")
            _check_json_value(item, field, errors)
        return
    errors.append(
        f"{field} must contain only JSON-compatible values "
        f"(got {type(value).__name__})"
    )


def is_expired(message: NexusMessage, now: Optional[datetime] = None) -> bool:
    """Return True iff ``message`` is expired at ``now`` (default: UTC now)."""
    if now is None:
        current = datetime.now(timezone.utc)
    else:
        current = _require_aware_now(now)
    return current >= message.expires_at


def validate_message(
    message: Any,
    *,
    check_expiry: bool = True,
    now: Optional[datetime] = None,
) -> list[str]:
    """Validate a typed message; return a list of errors (empty == valid)."""
    errors: list[str] = []
    if not isinstance(message, NexusMessage):
        return ["message must be a NexusMessage"]
    if now is not None:
        _require_aware_now(now)

    if message.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        errors.append(
            f"unsupported protocol_version: {message.protocol_version!r}"
        )

    if not isinstance(message.type, MessageType):
        errors.append(f"invalid message type: {message.type!r}")
        expected: Any = None
    else:
        try:
            expected = payload_type_for(message.type)
        except KeyError:
            errors.append(f"invalid message type: {message.type!r}")
            expected = None

    if not is_valid_message_id(message.message_id):
        errors.append(f"invalid message_id: {message.message_id!r}")
    if not is_valid_request_id(message.request_id):
        errors.append(f"invalid request_id: {message.request_id!r}")
    if not is_valid_agent_id(message.sender):
        errors.append(f"invalid sender: {message.sender!r}")
    if not is_valid_agent_id(message.recipient):
        errors.append(f"invalid recipient: {message.recipient!r}")

    if not _is_tz_aware(message.created_at):
        errors.append("created_at must be a timezone-aware datetime")
    if not _is_tz_aware(message.expires_at):
        errors.append("expires_at must be a timezone-aware datetime")
    if (
        _is_tz_aware(message.created_at)
        and _is_tz_aware(message.expires_at)
        and message.expires_at <= message.created_at
    ):
        errors.append("expires_at must be after created_at")
    elif check_expiry and _is_tz_aware(message.expires_at):
        current = now if now is not None else datetime.now(timezone.utc)
        if current >= message.expires_at:
            errors.append("message has expired")

    if expected is not None and not isinstance(message.payload, expected):
        errors.append(
            f"payload must be {expected.__name__} "
            f"for type {message.type.value}"
        )
    else:
        errors.extend(validate_payload(message.payload))

    if not isinstance(message.signature, str) or not message.signature:
        errors.append("signature must be a non-empty string")

    return errors


def assert_valid_message(
    message: NexusMessage,
    *,
    check_expiry: bool = True,
    now: Optional[datetime] = None,
) -> None:
    """Validate a typed message; raise :class:`ProtocolValidationError`."""
    errors = validate_message(message, check_expiry=check_expiry, now=now)
    if errors:
        raise ProtocolValidationError(errors)


def validate_payload(payload: Any) -> list[str]:
    """Validate a typed payload dataclass; return errors (empty == valid)."""
    if isinstance(payload, ConnectRequestPayload):
        return _validate_connect_request(payload)
    if isinstance(payload, ConnectResponsePayload):
        return _validate_connect_response(payload)
    if isinstance(payload, RemoteQueryPayload):
        return _validate_remote_query(payload)
    if isinstance(payload, ApprovalRequestPayload):
        return _validate_approval_request(payload)
    if isinstance(payload, ApprovalResponsePayload):
        return _validate_approval_response(payload)
    if isinstance(payload, RemoteResponsePayload):
        return _validate_remote_response(payload)
    if isinstance(payload, ErrorPayload):
        return _validate_error(payload)
    return [f"unknown payload type: {type(payload).__name__}"]


def validate_payload_dict(
    message_type: MessageType, payload: Any
) -> list[str]:
    """Validate a raw (dict) payload for ``message_type``.

    Used by deserialization before constructing typed payloads.
    """
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    if message_type is MessageType.CONNECT_REQUEST:
        return _validate_connect_request_dict(payload)
    if message_type is MessageType.CONNECT_RESPONSE:
        return _validate_connect_response_dict(payload)
    if message_type is MessageType.REMOTE_QUERY:
        return _validate_remote_query_dict(payload)
    if message_type is MessageType.APPROVAL_REQUEST:
        return _validate_approval_request_dict(payload)
    if message_type is MessageType.APPROVAL_RESPONSE:
        return _validate_approval_response_dict(payload)
    if message_type is MessageType.REMOTE_RESPONSE:
        return _validate_remote_response_dict(payload)
    if message_type is MessageType.ERROR:
        return _validate_error_dict(payload)
    return [f"invalid message type: {message_type!r}"]


# ---------------------------------------------------------------------------
# Typed payload validators
# ---------------------------------------------------------------------------


def _optional_str(value: Any, name: str, errors: list[str]) -> None:
    if value is not None and not isinstance(value, str):
        errors.append(f"{name} must be a string or null")


def _validate_capability_list(value: Any, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append("requested_capabilities must be a list")
        return
    for capability in value:
        if not is_valid_capability(capability):
            errors.append(
                f"invalid requested capability: {capability!r}"
            )


def _validate_connect_request(payload: ConnectRequestPayload) -> list[str]:
    errors: list[str] = []
    if not is_valid_agent_id(payload.agent_id):
        errors.append(f"invalid agent_id: {payload.agent_id!r}")
    _optional_str(payload.display_name, "display_name", errors)
    _optional_str(payload.context, "context", errors)
    return errors


def _validate_connect_response(payload: ConnectResponsePayload) -> list[str]:
    errors: list[str] = []
    if type(payload.accepted) is not bool:
        errors.append("accepted must be a boolean")
    if not is_valid_agent_id(payload.responder_agent_id):
        errors.append(
            f"invalid responder_agent_id: {payload.responder_agent_id!r}"
        )
    _optional_str(payload.reason, "reason", errors)
    return errors


def _validate_remote_query(payload: RemoteQueryPayload) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload.query, str) or not payload.query:
        errors.append("query must be a non-empty string")
    _validate_capability_list(payload.requested_capabilities, errors)
    return errors


def _validate_approval_request(payload: ApprovalRequestPayload) -> list[str]:
    errors: list[str] = []
    if not is_valid_request_id(payload.request_ref):
        errors.append(f"invalid request_ref: {payload.request_ref!r}")
    if not isinstance(payload.query, str) or not payload.query:
        errors.append("query must be a non-empty string")
    _validate_capability_list(payload.requested_capabilities, errors)
    _optional_str(payload.context, "context", errors)
    return errors


def _validate_approval_response(payload: ApprovalResponsePayload) -> list[str]:
    errors: list[str] = []
    if type(payload.approved) is not bool:
        errors.append("approved must be a boolean")
    _optional_str(payload.reason, "reason", errors)
    return errors


def _validate_remote_response(payload: RemoteResponsePayload) -> list[str]:
    errors: list[str] = []
    if payload.status not in REMOTE_RESPONSE_STATUSES:
        errors.append(f"invalid status: {payload.status!r}")
    if not isinstance(payload.answer, str):
        errors.append("answer must be a string")
    elif payload.status == "ok" and not payload.answer:
        errors.append("answer must be a non-empty string when status is 'ok'")
    if payload.structured_data is not None:
        if not isinstance(payload.structured_data, dict):
            errors.append("structured_data must be an object or null")
        else:
            _check_json_value(
                payload.structured_data, "structured_data", errors
            )
    if payload.citations is not None:
        if not isinstance(payload.citations, list) or any(
            not isinstance(citation, str) for citation in payload.citations
        ):
            errors.append("citations must be a list of strings or null")
    _optional_str(payload.error_code, "error_code", errors)
    _optional_str(payload.error_message, "error_message", errors)
    return errors


def _validate_error(payload: ErrorPayload) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload.error_code, str) or (
        _ERROR_CODE_RE.match(payload.error_code) is None
    ):
        errors.append(f"invalid error_code: {payload.error_code!r}")
    if not isinstance(payload.message, str) or not payload.message:
        errors.append("message must be a non-empty string")
    if type(payload.retryable) is not bool:
        errors.append("retryable must be a boolean")
    return errors


# ---------------------------------------------------------------------------
# Raw-dict payload validators (no silent coercion: wrong types are errors)
# ---------------------------------------------------------------------------


def _check_exact_keys(
    payload: dict[str, Any], required: set[str], optional: set[str]
) -> list[str]:
    errors: list[str] = []
    for name in sorted(set(payload) - required - optional):
        errors.append(f"unknown payload field: {name!r}")
    for name in sorted(required - set(payload)):
        errors.append(f"missing payload field: {name!r}")
    return errors


def _validate_connect_request_dict(payload: dict[str, Any]) -> list[str]:
    errors = _check_exact_keys(
        payload, {"agent_id"}, {"display_name", "context"}
    )
    if "agent_id" in payload and not is_valid_agent_id(payload["agent_id"]):
        errors.append(f"invalid agent_id: {payload['agent_id']!r}")
    for name in ("display_name", "context"):
        if name in payload:
            _optional_str(payload[name], name, errors)
    return errors


def _validate_connect_response_dict(payload: dict[str, Any]) -> list[str]:
    errors = _check_exact_keys(
        payload, {"accepted", "responder_agent_id"}, {"reason"}
    )
    if "accepted" in payload and type(payload["accepted"]) is not bool:
        errors.append("accepted must be a boolean")
    if "responder_agent_id" in payload and not is_valid_agent_id(
        payload["responder_agent_id"]
    ):
        errors.append(
            "invalid responder_agent_id: "
            f"{payload['responder_agent_id']!r}"
        )
    if "reason" in payload:
        _optional_str(payload["reason"], "reason", errors)
    return errors


def _validate_remote_query_dict(payload: dict[str, Any]) -> list[str]:
    errors = _check_exact_keys(
        payload, {"query", "requested_capabilities"}, set()
    )
    if "query" in payload and (
        not isinstance(payload["query"], str) or not payload["query"]
    ):
        errors.append("query must be a non-empty string")
    if "requested_capabilities" in payload:
        _validate_capability_list(payload["requested_capabilities"], errors)
    return errors


def _validate_approval_request_dict(payload: dict[str, Any]) -> list[str]:
    errors = _check_exact_keys(
        payload,
        {"request_ref", "query", "requested_capabilities"},
        {"context"},
    )
    if "request_ref" in payload and not is_valid_request_id(
        payload["request_ref"]
    ):
        errors.append(f"invalid request_ref: {payload['request_ref']!r}")
    if "query" in payload and (
        not isinstance(payload["query"], str) or not payload["query"]
    ):
        errors.append("query must be a non-empty string")
    if "requested_capabilities" in payload:
        _validate_capability_list(payload["requested_capabilities"], errors)
    if "context" in payload:
        _optional_str(payload["context"], "context", errors)
    return errors


def _validate_approval_response_dict(payload: dict[str, Any]) -> list[str]:
    errors = _check_exact_keys(payload, {"approved"}, {"reason"})
    if "approved" in payload and type(payload["approved"]) is not bool:
        errors.append("approved must be a boolean")
    if "reason" in payload:
        _optional_str(payload["reason"], "reason", errors)
    return errors


def _validate_remote_response_dict(payload: dict[str, Any]) -> list[str]:
    errors = _check_exact_keys(
        payload,
        {"status", "answer"},
        {"structured_data", "citations", "error_code", "error_message"},
    )
    if "status" in payload and payload["status"] not in (
        REMOTE_RESPONSE_STATUSES
    ):
        errors.append(f"invalid status: {payload['status']!r}")
    if "answer" in payload:
        if not isinstance(payload["answer"], str):
            errors.append("answer must be a string")
        elif payload.get("status") == "ok" and not payload["answer"]:
            errors.append(
                "answer must be a non-empty string when status is 'ok'"
            )
    if "structured_data" in payload and payload["structured_data"] is not None:
        if not isinstance(payload["structured_data"], dict):
            errors.append("structured_data must be an object or null")
        else:
            _check_json_value(
                payload["structured_data"], "structured_data", errors
            )
    if "citations" in payload and payload["citations"] is not None:
        if not isinstance(payload["citations"], list) or any(
            not isinstance(citation, str)
            for citation in payload["citations"]
        ):
            errors.append("citations must be a list of strings or null")
    for name in ("error_code", "error_message"):
        if name in payload:
            _optional_str(payload[name], name, errors)
    return errors


def _validate_error_dict(payload: dict[str, Any]) -> list[str]:
    errors = _check_exact_keys(
        payload, {"error_code", "message", "retryable"}, set()
    )
    if "error_code" in payload and (
        not isinstance(payload["error_code"], str)
        or _ERROR_CODE_RE.match(payload["error_code"]) is None
    ):
        errors.append(f"invalid error_code: {payload['error_code']!r}")
    if "message" in payload and (
        not isinstance(payload["message"], str) or not payload["message"]
    ):
        errors.append("message must be a non-empty string")
    if "retryable" in payload and type(payload["retryable"]) is not bool:
        errors.append("retryable must be a boolean")
    return errors
