"""Validation tests: valid messages pass, invalid messages fail clearly."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.protocol.models import (
    ApprovalRequestPayload,
    ApprovalResponsePayload,
    ConnectRequestPayload,
    ConnectResponsePayload,
    ErrorPayload,
    NexusMessage,
    RemoteQueryPayload,
    RemoteResponsePayload,
)
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import (
    ProtocolValidationError,
    assert_valid_message,
    is_expired,
    validate_message,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)


def _envelope(message_type, payload, **overrides):
    fields = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": "msg_001",
        "request_id": "req_123",
        "sender": "agent:alice",
        "recipient": "agent:bob",
        "type": message_type,
        "created_at": BASE,
        "expires_at": BASE + timedelta(hours=1),
        "payload": payload,
        "signature": "sig-placeholder",
    }
    fields.update(overrides)
    return NexusMessage(**fields)


def _all_valid_messages():
    return [
        _envelope(
            MessageType.CONNECT_REQUEST,
            ConnectRequestPayload(agent_id="agent:bob"),
        ),
        _envelope(
            MessageType.CONNECT_RESPONSE,
            ConnectResponsePayload(
                accepted=True, responder_agent_id="agent:bob"
            ),
        ),
        _envelope(
            MessageType.REMOTE_QUERY,
            RemoteQueryPayload(
                query="What projects are you working on?",
                requested_capabilities=["memory.read"],
            ),
        ),
        _envelope(
            MessageType.APPROVAL_REQUEST,
            ApprovalRequestPayload(
                request_ref="req_123",
                query="What projects are you working on?",
                requested_capabilities=["memory.read"],
            ),
        ),
        _envelope(
            MessageType.APPROVAL_RESPONSE,
            ApprovalResponsePayload(approved=False, reason="no"),
        ),
        _envelope(
            MessageType.REMOTE_RESPONSE,
            RemoteResponsePayload(status="ok", answer="Project Nexus."),
        ),
        _envelope(
            MessageType.ERROR,
            ErrorPayload(
                error_code="NOT_FOUND", message="gone", retryable=False
            ),
        ),
    ]


@pytest.mark.parametrize("message", _all_valid_messages())
def test_all_valid_messages_pass_validation(message):  # cases 1-7
    assert validate_message(message, now=NOW) == []


def test_invalid_protocol_version_rejected():  # case 9
    msg = _all_valid_messages()[0]
    msg.protocol_version = "2.0"
    errors = validate_message(msg, now=NOW)
    assert any("protocol_version" in error for error in errors)
    with pytest.raises(ProtocolValidationError):
        assert_valid_message(msg, now=NOW)


def test_invalid_message_type_rejected():  # case 10
    msg = _all_valid_messages()[2]
    msg.type = "TELEPATHY"  # type: ignore[assignment]
    errors = validate_message(msg, now=NOW)
    assert any("message type" in error for error in errors)


def test_invalid_message_id_rejected():  # case 11
    msg = _all_valid_messages()[0]
    msg.message_id = "not a valid id!!"
    assert any(
        "message_id" in error for error in validate_message(msg, now=NOW)
    )


def test_invalid_request_id_rejected():  # case 12
    msg = _all_valid_messages()[0]
    msg.request_id = ""
    assert any(
        "request_id" in error for error in validate_message(msg, now=NOW)
    )


def test_invalid_sender_rejected():  # case 13
    msg = _all_valid_messages()[0]
    msg.sender = "   "
    assert any(
        "sender" in error for error in validate_message(msg, now=NOW)
    )


def test_invalid_recipient_rejected():  # case 14
    msg = _all_valid_messages()[0]
    msg.recipient = ""
    assert any(
        "recipient" in error for error in validate_message(msg, now=NOW)
    )


def test_naive_timestamp_rejected():  # case 15 (typed level)
    msg = _all_valid_messages()[0]
    msg.created_at = datetime(2026, 1, 1, 12, 0, 0)  # naive
    assert any(
        "created_at" in error for error in validate_message(msg, now=NOW)
    )


def test_expiry_ordering_rejected():
    msg = _all_valid_messages()[0]
    msg.expires_at = BASE - timedelta(seconds=1)
    assert any(
        "expires_at" in error for error in validate_message(msg, now=NOW)
    )


def test_expired_message_detected():  # case 16
    msg = _all_valid_messages()[0]
    assert is_expired(msg, now=NOW) is False
    assert is_expired(msg, now=BASE + timedelta(hours=2)) is True
    errors = validate_message(msg, now=BASE + timedelta(hours=2))
    assert any("expired" in error for error in errors)


def test_invalid_payload_capabilities_not_list():  # case 17
    msg = _envelope(
        MessageType.REMOTE_QUERY,
        RemoteQueryPayload(query="hi", requested_capabilities=["memory.read"]),
    )
    msg.payload.requested_capabilities = "memory.read"  # type: ignore
    errors = validate_message(msg, now=NOW)
    assert any("requested_capabilities must be a list" in e for e in errors)


def test_invalid_payload_wrong_type_for_message():
    msg = _envelope(
        MessageType.REMOTE_QUERY,
        ErrorPayload(error_code="X", message="y", retryable=False),
    )
    assert any("payload must be" in e for e in validate_message(msg, now=NOW))


def test_missing_signature_rejected():  # case 19 (typed level)
    msg = _all_valid_messages()[0]
    msg.signature = ""
    assert any(
        "signature" in error for error in validate_message(msg, now=NOW)
    )


def test_invalid_error_structure_rejected():  # case 20
    msg = _envelope(
        MessageType.ERROR,
        ErrorPayload(error_code="bad code!", message="", retryable="yes"),
    )
    errors = validate_message(msg, now=NOW)
    assert any("error_code" in error for error in errors)
    assert any("message must be" in error for error in errors)
    assert any("retryable" in error for error in errors)


def test_no_silent_coercion_of_booleans():
    msg = _envelope(
        MessageType.APPROVAL_RESPONSE,
        ApprovalResponsePayload(approved=True),
    )
    msg.payload.approved = 1  # type: ignore[assignment]
    assert any(
        "approved must be a boolean" in e
        for e in validate_message(msg, now=NOW)
    )
