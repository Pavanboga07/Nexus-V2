"""Message-builder tests: valid envelopes out, garbage rejected (no DB)."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.protocol.models import RemoteQueryPayload
from backend.protocol.types import MessageType
from backend.protocol.validation import validate_message
from backend.requests.errors import RequestValidationError
from backend.requests.message_builder import build_remote_query_message

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _build(**over):
    fields = {
        "sender_agent_id": "agent:alice",
        "recipient_agent_id": "agent:bob",
        "request_id": "req_1",
        "message_id": "msg_1",
        "query": "What projects are you working on?",
        "requested_capabilities": ["memory.read"],
        "created_at": BASE,
        "expires_at": BASE + timedelta(hours=1),
    }
    fields.update(over)
    return build_remote_query_message(**fields)


def test_valid_remote_query_built():
    message = _build()
    assert message.type is MessageType.REMOTE_QUERY
    assert isinstance(message.payload, RemoteQueryPayload)
    assert message.payload.requested_capabilities == ["memory.read"]
    assert message.signature == ""  # unsigned: service signs next
    candidate_errors = validate_message(message, check_expiry=False)
    assert [e for e in candidate_errors if "signature" not in e] == []


def test_empty_query_rejected():
    with pytest.raises(RequestValidationError):
        _build(query="")


def test_bad_capabilities_rejected():
    with pytest.raises(RequestValidationError):
        _build(requested_capabilities="memory.read")  # type: ignore[arg-type]


def test_bad_ids_rejected():
    with pytest.raises(RequestValidationError):
        _build(request_id="not valid!!")
    with pytest.raises(RequestValidationError):
        _build(message_id="")


def test_bad_timestamps_rejected():
    with pytest.raises(RequestValidationError):
        _build(expires_at=BASE - timedelta(seconds=1))
    with pytest.raises(RequestValidationError):
        _build(created_at=datetime(2026, 1, 1))  # naive
