"""Model tests: envelope + all seven payload types construct correctly."""

from datetime import datetime, timedelta, timezone

from backend.protocol.models import (
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
from backend.protocol.types import (
    PROTOCOL_VERSION,
    MessageType,
    generate_message_id,
    generate_request_id,
    is_valid_message_id,
    is_valid_request_id,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


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


def test_protocol_version_is_1_0():
    assert PROTOCOL_VERSION == "1.0"


def test_message_type_enum_has_exactly_seven_types():
    assert {t.value for t in MessageType} == {
        "CONNECT_REQUEST",
        "CONNECT_RESPONSE",
        "REMOTE_QUERY",
        "APPROVAL_REQUEST",
        "APPROVAL_RESPONSE",
        "REMOTE_RESPONSE",
        "ERROR",
    }


def test_payload_type_mapping_covers_all_types():
    for message_type in MessageType:
        assert payload_type_for(message_type) is not None


def test_valid_connect_request_model():  # case 1
    msg = _envelope(
        MessageType.CONNECT_REQUEST,
        ConnectRequestPayload(
            agent_id="agent:bob", display_name="Bob", context="collab"
        ),
    )
    assert msg.payload.agent_id == "agent:bob"
    assert msg.request_id == "req_123"


def test_valid_connect_response_model():  # case 2
    msg = _envelope(
        MessageType.CONNECT_RESPONSE,
        ConnectResponsePayload(
            accepted=True, responder_agent_id="agent:bob"
        ),
    )
    assert msg.payload.accepted is True


def test_valid_remote_query_model():  # case 3
    msg = _envelope(
        MessageType.REMOTE_QUERY,
        RemoteQueryPayload(
            query="What projects are you working on?",
            requested_capabilities=["memory.read"],
        ),
    )
    assert msg.payload.requested_capabilities == ["memory.read"]


def test_valid_approval_request_model():  # case 4
    msg = _envelope(
        MessageType.APPROVAL_REQUEST,
        ApprovalRequestPayload(
            request_ref="req_123",
            query="What projects are you working on?",
            requested_capabilities=["memory.read"],
        ),
    )
    assert msg.payload.request_ref == "req_123"


def test_valid_approval_response_model():  # case 5
    msg = _envelope(
        MessageType.APPROVAL_RESPONSE,
        ApprovalResponsePayload(approved=True, reason="looks fine"),
    )
    assert msg.payload.approved is True


def test_valid_remote_response_model():  # case 6
    msg = _envelope(
        MessageType.REMOTE_RESPONSE,
        RemoteResponsePayload(
            status="ok",
            answer="Project Nexus.",
            structured_data={"projects": ["nexus"]},
            citations=["memory:001"],
        ),
    )
    assert msg.payload.status == "ok"


def test_valid_error_model():  # case 7
    msg = _envelope(
        MessageType.ERROR,
        ErrorPayload(
            error_code="NOT_FOUND", message="no such agent", retryable=False
        ),
    )
    assert msg.payload.error_code == "NOT_FOUND"


def test_optional_payload_fields_default_to_none():
    assert ApprovalResponsePayload(approved=False).reason is None
    assert ConnectRequestPayload(agent_id="agent:x").display_name is None
    response = RemoteResponsePayload(status="ok", answer="hi")
    assert response.structured_data is None
    assert response.citations is None


def test_generated_ids_are_valid_and_unique():
    first, second = generate_message_id(), generate_message_id()
    assert is_valid_message_id(first)
    assert first != second
    req_first, req_second = generate_request_id(), generate_request_id()
    assert is_valid_request_id(req_first)
    assert req_first != req_second
