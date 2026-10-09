"""Hardening tests for the five audited fixes (adversarial, read-only repo)."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.protocol.canonicalization import canonicalize_message
from backend.protocol.models import (
    ErrorPayload,
    NexusMessage,
    RemoteQueryPayload,
    RemoteResponsePayload,
)
from backend.protocol.serialization import (
    ProtocolSerializationError,
    message_from_dict,
    message_from_json,
    message_to_dict,
    message_to_json,
)
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import (
    ProtocolValidationError,
    is_expired,
    validate_message,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)


def _query(**overrides):
    fields = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": "msg_001",
        "request_id": "req_123",
        "sender": "agent:alice",
        "recipient": "agent:bob",
        "type": MessageType.REMOTE_QUERY,
        "created_at": BASE,
        "expires_at": BASE + timedelta(hours=1),
        "payload": RemoteQueryPayload(
            query="What projects are you working on?",
            requested_capabilities=["memory.read"],
        ),
        "signature": "sig-placeholder",
    }
    fields.update(overrides)
    return NexusMessage(**fields)


def _response(structured_data):
    return _query(
        message_id="msg_003",
        type=MessageType.REMOTE_RESPONSE,
        payload=RemoteResponsePayload(
            status="ok", answer="x", structured_data=structured_data
        ),
    )


def _wire(**overrides):
    data = {
        "protocol_version": "1.0",
        "message_id": "msg_001",
        "request_id": "req_123",
        "sender": "agent:alice",
        "recipient": "agent:bob",
        "type": "REMOTE_RESPONSE",
        "created_at": "2026-01-01T12:00:00+00:00",
        "expires_at": "2026-01-01T13:00:00+00:00",
        "payload": {
            "status": "ok",
            "answer": "x",
            "structured_data": {"k": "v"},
        },
        "signature": "sig",
    }
    data.update(overrides)
    return data


def test_mutation_after_canonicalization():  # 1
    message = _query()
    snapshot = canonicalize_message(message)
    message.payload.query = "MUTATED"
    message.payload.requested_capabilities.append("admin.write")
    assert canonicalize_message(message) != snapshot
    # The snapshot itself is immutable bytes: signing it signs the
    # pre-mutation content, so sign-then-mutate is always detectable.
    assert snapshot == canonicalize_message(_query())


def test_equivalent_timestamp_spellings_identical_bytes():  # 2
    variants = [
        "2026-01-01T12:00:00+00:00",
        "2026-01-01T12:00:00Z",
        "2026-01-01T07:00:00-05:00",
        "2026-01-01T12:00:00.000000+00:00",
    ]
    canonical = [
        canonicalize_message(_wire(created_at=variant)) for variant in variants
    ]
    assert len(set(canonical)) == 1


def test_unicode_emoji_round_trip():  # 3
    text = "caf\u00e9 \U0001f600 \u4e2d\u6587 \u03bb"
    message = _query(
        payload=RemoteQueryPayload(
            query=text, requested_capabilities=["memory.read"]
        )
    )
    restored = message_from_json(message_to_json(message))
    assert restored.payload.query == text
    assert canonicalize_message(message) == canonicalize_message(restored)


def test_expires_equal_created_rejected():  # 4
    with pytest.raises(
        ProtocolSerializationError, match="expires_at must be after"
    ):
        message_from_dict(
            _wire(
                created_at="2026-01-01T12:00:00+00:00",
                expires_at="2026-01-01T12:00:00+00:00",
            )
        )
    message = _query(expires_at=BASE)
    assert any(
        "expires_at must be after" in e
        for e in validate_message(message, now=NOW, check_expiry=False)
    )


def test_naive_now_rejected():  # 5
    message = _query()
    naive = datetime(2026, 1, 1, 12, 30, 0)
    with pytest.raises(ProtocolValidationError, match="timezone-aware"):
        is_expired(message, now=naive)
    with pytest.raises(ProtocolValidationError, match="timezone-aware"):
        validate_message(message, now=naive)


def test_nonserializable_object_rejected():  # 6
    message = _response({"x": object()})
    assert any(
        "JSON-compatible" in e for e in validate_message(message, now=NOW)
    )
    with pytest.raises(ProtocolSerializationError, match="JSON-compatible"):
        message_from_dict(
            _wire(
                payload={
                    "status": "ok",
                    "answer": "x",
                    "structured_data": {"x": object()},
                }
            )
        )


def test_set_rejected():  # 7
    message = _response({"x": {1, 2}})
    assert any(
        "JSON-compatible" in e for e in validate_message(message, now=NOW)
    )


def test_nan_rejected():  # 8
    message = _response({"nested": {"v": float("nan")}})
    assert any(
        "non-finite" in e for e in validate_message(message, now=NOW)
    )
    nan_wire = (
        '{"protocol_version":"1.0","message_id":"m1","request_id":"r1",'
        '"sender":"a","recipient":"b","type":"REMOTE_RESPONSE",'
        '"created_at":"2026-01-01T12:00:00+00:00",'
        '"expires_at":"2026-01-01T13:00:00+00:00",'
        '"payload":{"status":"ok","answer":"x",'
        '"structured_data":{"v":NaN}},"signature":"s"}'
    )
    with pytest.raises(ProtocolSerializationError, match="non-finite"):
        message_from_json(nan_wire)


def test_positive_infinity_rejected():  # 9
    message = _response({"v": float("inf")})
    assert any(
        "non-finite" in e for e in validate_message(message, now=NOW)
    )


def test_negative_infinity_rejected():  # 10
    message = _response({"v": float("-inf")})
    assert any(
        "non-finite" in e for e in validate_message(message, now=NOW)
    )


def test_lone_surrogate_rejected():  # 11
    message = _response({"t": "\ud800"})
    assert any(
        "lone surrogates" in e for e in validate_message(message, now=NOW)
    )
    surrogate_wire = (
        '{"protocol_version":"1.0","message_id":"m1","request_id":"r1",'
        '"sender":"a","recipient":"b","type":"REMOTE_RESPONSE",'
        '"created_at":"2026-01-01T12:00:00+00:00",'
        '"expires_at":"2026-01-01T13:00:00+00:00",'
        '"payload":{"status":"ok","answer":"x",'
        '"structured_data":{"t":"\\ud800"}},"signature":"s"}'
    )
    with pytest.raises(ProtocolSerializationError, match="lone surrogates"):
        message_from_json(surrogate_wire)


def test_invalid_typed_message_rejected_by_to_dict():  # 12
    message = _query()
    message.type = "BOGUS"  # type: ignore[assignment]
    with pytest.raises(
        ProtocolSerializationError, match="invalid message type"
    ):
        message_to_dict(message)


def test_payload_type_mismatch_rejected_by_to_dict():  # 13
    message = _query(
        payload=ErrorPayload(
            error_code="X", message="y", retryable=False
        )
    )
    with pytest.raises(
        ProtocolSerializationError, match="payload must be"
    ):
        message_to_dict(message)


def test_duplicate_identifier_semantics():  # 14
    first = _query(message_id="msg_001", request_id="req_123")
    second = _query(message_id="msg_001", request_id="req_123")
    # The protocol layer keeps no registry: duplicates validate (dedup
    # belongs to persistence/gateway), while correlation still holds.
    assert validate_message(first, now=NOW) == []
    assert validate_message(second, now=NOW) == []
    assert first.request_id == second.request_id == "req_123"


def test_serialization_errors_wrapped():  # 15
    message = _response({"k": "v"})
    assert validate_message(message, now=NOW) == []
    message.payload.structured_data = {"x": object()}  # type: ignore[dict-item]
    with pytest.raises(ProtocolSerializationError) as exc_info:
        message_to_json(message)
    assert exc_info.value.errors


def test_canonicalization_encoding_errors_wrapped():  # 16
    message = _response({"k": "v"})
    message.payload.structured_data = {"t": "\ud800"}  # type: ignore[dict-item]
    with pytest.raises(ProtocolSerializationError) as exc_info:
        canonicalize_message(message)
    assert exc_info.value.errors
    message.payload.structured_data = {"x": object()}  # type: ignore[dict-item]
    with pytest.raises(ProtocolSerializationError):
        canonicalize_message(message)
