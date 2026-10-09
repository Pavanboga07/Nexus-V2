"""Signing tests: valid signing, encoding, determinism, misuse."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from backend.identity.errors import (
    IdentityError,
    SenderMismatchError,
)
from backend.identity.identity import create_identity
from backend.identity.signing import SIGNATURE_LENGTH, sign_message
from backend.identity.verification import verify_message
from backend.protocol.models import ErrorPayload, NexusMessage, RemoteQueryPayload
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import ProtocolValidationError

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)
B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _message(identity, **overrides):
    fields = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": "msg_001",
        "request_id": "req_123",
        "sender": identity.agent_id,
        "recipient": "agent:peer",
        "type": MessageType.REMOTE_QUERY,
        "created_at": BASE,
        "expires_at": BASE + timedelta(hours=1),
        "payload": RemoteQueryPayload(
            query="What projects are you working on?",
            requested_capabilities=["memory.read"],
        ),
        "signature": "",
    }
    fields.update(overrides)
    return NexusMessage(**fields)


def test_valid_message_signs():  # 17
    identity = create_identity()
    unsigned = _message(identity)
    signed = sign_message(unsigned, identity.private_key)
    assert isinstance(signed, NexusMessage)
    assert signed.signature
    assert unsigned.signature == ""  # original never mutated


def test_signature_is_non_empty():  # 18
    identity = create_identity()
    assert isinstance(sign_message(_message(identity), identity.private_key).signature, str)
    assert sign_message(_message(identity), identity.private_key).signature != ""


def test_signature_encoding_is_valid_base64url():  # 19
    identity = create_identity()
    signature = sign_message(_message(identity), identity.private_key).signature
    assert B64URL_RE.match(signature) is not None
    assert "=" not in signature
    from backend.identity.signing import decode_signature

    assert len(decode_signature(signature)) == SIGNATURE_LENGTH


def test_signing_does_not_alter_signed_fields():  # 20
    identity = create_identity()
    unsigned = _message(identity)
    signed = sign_message(unsigned, identity.private_key)
    for field_name in (
        "protocol_version", "message_id", "request_id", "sender",
        "recipient", "type", "created_at", "expires_at", "payload",
    ):
        assert getattr(signed, field_name) == getattr(unsigned, field_name)


def test_signed_message_verifies():  # 21
    identity = create_identity()
    signed = sign_message(_message(identity), identity.private_key)
    result = verify_message(signed, identity.public_key, now=NOW)
    assert result.agent_id == identity.agent_id
    assert result.message_id == "msg_001"
    assert result.request_id == "req_123"


def test_signing_same_message_gives_valid_signature():  # 22
    identity = create_identity()
    first = sign_message(_message(identity), identity.private_key)
    second = sign_message(_message(identity), identity.private_key)
    assert first.signature == second.signature  # Ed25519 deterministic
    verify_message(first, identity.public_key, now=NOW)
    verify_message(second, identity.public_key, now=NOW)


def test_unsigned_message_fails_verification():  # 23
    identity = create_identity()
    with pytest.raises(ProtocolValidationError):
        verify_message(_message(identity), identity.public_key, now=NOW)
    tampered = sign_message(_message(identity), identity.private_key)
    tampered = NexusMessage(
        **{**tampered.__dict__, "signature": "garbage!!!"}
    )
    with pytest.raises(IdentityError):
        verify_message(tampered, identity.public_key, now=NOW)


def test_signing_with_mismatched_sender_rejected():
    first, second = create_identity(), create_identity()
    message = _message(first, sender=second.agent_id)
    with pytest.raises(SenderMismatchError):
        sign_message(message, first.private_key)


def test_signing_rejects_non_message():
    identity = create_identity()
    with pytest.raises(TypeError):
        sign_message({"not": "a message"}, identity.private_key)  # type: ignore[arg-type]


def test_signing_rejects_structurally_invalid_message():
    identity = create_identity()
    message = _message(
        identity,
        payload=ErrorPayload(error_code="X", message="y", retryable=False),
    )
    with pytest.raises(ProtocolValidationError):
        sign_message(message, identity.private_key)
