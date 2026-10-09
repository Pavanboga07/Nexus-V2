"""Serialization tests: typed message <-> JSON, strict both directions."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.serialization import (
    ProtocolSerializationError,
    message_from_dict,
    message_from_json,
    message_to_dict,
    message_to_json,
)
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import assert_valid_message

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)


def _message(**overrides):
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


def test_serialize_deserialize_round_trip():  # case 21
    original = _message()
    restored = message_from_json(message_to_json(original))
    assert restored == original
    assert_valid_message(restored, now=NOW)


def test_deserialize_serialize_consistency():  # case 22
    first = message_to_dict(_message())
    second = message_to_dict(message_from_dict(first))
    assert first == second


def test_typed_payload_preserved():  # case 23
    restored = message_from_dict(message_to_dict(_message()))
    assert isinstance(restored.payload, RemoteQueryPayload)
    assert restored.payload.query == "What projects are you working on?"
    assert restored.payload.requested_capabilities == ["memory.read"]
    assert restored.type is MessageType.REMOTE_QUERY


def test_malformed_json_rejected():  # cases 18 + 24
    with pytest.raises(ProtocolSerializationError, match="malformed JSON"):
        message_from_json("{not valid json")


def test_non_object_json_rejected():
    with pytest.raises(ProtocolSerializationError):
        message_from_json("[1, 2, 3]")


def test_missing_required_field_rejected():  # case 8
    data = message_to_dict(_message())
    del data["payload"]
    with pytest.raises(
        ProtocolSerializationError, match="missing envelope field"
    ):
        message_from_dict(data)


def test_missing_signature_field_rejected():  # case 19 (wire level)
    data = message_to_dict(_message())
    del data["signature"]
    with pytest.raises(
        ProtocolSerializationError, match="missing envelope field"
    ):
        message_from_dict(data)


def test_unknown_envelope_field_rejected():
    data = message_to_dict(_message())
    data["transport"] = "websocket"
    with pytest.raises(
        ProtocolSerializationError, match="unknown envelope field"
    ):
        message_from_dict(data)


def test_unsupported_protocol_version_rejected():  # case 9 (wire level)
    data = message_to_dict(_message())
    data["protocol_version"] = "9.9"
    with pytest.raises(
        ProtocolSerializationError, match="unsupported protocol_version"
    ):
        message_from_dict(data)


def test_invalid_message_type_rejected():  # case 10 (wire level)
    data = message_to_dict(_message())
    data["type"] = "MIND Meld"
    with pytest.raises(
        ProtocolSerializationError, match="invalid message type"
    ):
        message_from_dict(data)


def test_malformed_timestamp_rejected():  # case 15 (wire level)
    data = message_to_dict(_message())
    data["created_at"] = "yesterday-ish"
    with pytest.raises(
        ProtocolSerializationError, match="invalid timestamp"
    ):
        message_from_dict(data)


def test_naive_timestamp_rejected():
    data = message_to_dict(_message())
    data["created_at"] = "2026-01-01T12:00:00"
    with pytest.raises(
        ProtocolSerializationError, match="timezone required"
    ):
        message_from_dict(data)


def test_invalid_ids_rejected():  # cases 11-14 (wire level)
    for field in ("message_id", "request_id", "sender", "recipient"):
        data = message_to_dict(_message())
        data[field] = ""
        with pytest.raises(
            ProtocolSerializationError, match=f"invalid {field}"
        ):
            message_from_dict(data)


def test_invalid_payload_rejected():  # case 17 (wire level)
    data = message_to_dict(_message())
    data["payload"] = {
        "query": "hi",
        "requested_capabilities": "memory.read",  # must be a list
    }
    with pytest.raises(
        ProtocolSerializationError,
        match="requested_capabilities must be a list",
    ):
        message_from_dict(data)
