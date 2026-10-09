"""Canonicalization + correlation tests."""

import json
from datetime import timedelta, timezone

from backend.protocol.canonicalization import (
    canonicalize_message,
    message_to_canonical_dict,
)
from backend.protocol.models import (
    ApprovalRequestPayload,
    RemoteQueryPayload,
    RemoteResponsePayload,
)
from backend.protocol.serialization import message_from_json, message_to_json
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import validate_message

from datetime import datetime

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)


def _query_message(message_id="msg_001", request_id="req_123", **overrides):
    from backend.protocol.models import NexusMessage

    fields = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": message_id,
        "request_id": request_id,
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


def test_identical_message_produces_identical_bytes():  # case 25
    first = canonicalize_message(_query_message())
    second = canonicalize_message(_query_message())
    assert first == second
    assert isinstance(first, bytes)


def test_field_insertion_order_does_not_matter():  # case 26
    from backend.protocol.serialization import message_to_dict

    data = message_to_dict(_query_message())
    reversed_data = dict(reversed(list(data.items())))
    assert list(reversed_data) != list(data)
    assert (
        canonicalize_message(reversed_data) == canonicalize_message(data)
    )


def test_whitespace_does_not_matter():  # case 27
    compact = message_to_json(_query_message())
    spaced = json.dumps(json.loads(compact), indent=4, sort_keys=True)
    assert spaced != compact
    assert (
        canonicalize_message(message_from_json(spaced))
        == canonicalize_message(message_from_json(compact))
    )


def test_signature_excluded_from_canonical_bytes():  # case 28
    first = _query_message(signature="sig-one")
    second = _query_message(signature="sig-two")
    assert canonicalize_message(first) == canonicalize_message(second)
    assert b"sig-one" not in canonicalize_message(first)
    assert b"sig-two" not in canonicalize_message(second)
    assert "signature" not in message_to_canonical_dict(first)


def test_changing_signed_field_changes_bytes():  # case 29
    original = canonicalize_message(_query_message())
    altered = _query_message()
    altered.payload.query = "Something completely different"
    assert canonicalize_message(altered) != original


def test_changing_request_id_changes_bytes():  # case 30
    original = canonicalize_message(_query_message(request_id="req_123"))
    altered = canonicalize_message(_query_message(request_id="req_999"))
    assert original != altered


def test_multiple_messages_may_share_one_request_id():  # case 31
    from backend.protocol.models import NexusMessage

    query = _query_message(message_id="msg_001", request_id="req_123")
    approval = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id="msg_002",
        request_id="req_123",
        sender="agent:bob",
        recipient="agent:alice",
        type=MessageType.APPROVAL_REQUEST,
        created_at=BASE,
        expires_at=BASE + timedelta(hours=1),
        payload=ApprovalRequestPayload(
            request_ref="req_123",
            query="What projects are you working on?",
            requested_capabilities=["memory.read"],
        ),
        signature="sig-placeholder",
    )
    response = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id="msg_003",
        request_id="req_123",
        sender="agent:bob",
        recipient="agent:alice",
        type=MessageType.REMOTE_RESPONSE,
        created_at=BASE,
        expires_at=BASE + timedelta(hours=1),
        payload=RemoteResponsePayload(status="ok", answer="Project Nexus."),
        signature="sig-placeholder",
    )
    for message in (query, approval, response):
        assert validate_message(message, now=NOW) == []
    assert len({query.message_id, approval.message_id}) == 2  # case 32
    assert response.request_id == query.request_id == "req_123"  # case 33
    assert len(
        {
            canonicalize_message(query),
            canonicalize_message(approval),
            canonicalize_message(response),
        }
    ) == 3
