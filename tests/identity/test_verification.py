"""Verification tests: tampering, canonicalization, expiry, errors."""

import base64
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.identity.errors import (
    ExpiredMessageError,
    IdentityError,
    InvalidPublicKeyError,
    InvalidSignatureError,
    MalformedSignatureError,
    SenderMismatchError,
)
from backend.identity.identity import create_identity
from backend.identity.keys import private_key_to_bytes
from backend.identity.signing import decode_signature, sign_message
from backend.identity.verification import verify_message
from backend.protocol.canonicalization import canonicalize_message
from backend.protocol.models import NexusMessage, RemoteQueryPayload, RemoteResponsePayload
from backend.protocol.serialization import (
    ProtocolSerializationError,
    message_from_dict,
    message_from_json,
    message_to_dict,
)
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import ProtocolValidationError

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(minutes=1)
LATE = BASE + timedelta(hours=2)


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


def _signed(identity=None):
    identity = identity or create_identity()
    return identity, sign_message(_message(identity), identity.private_key)


def _mutate(signed, **changes):
    return dataclasses.replace(signed, **changes)


def _tamper_cases():
    other = create_identity().agent_id
    return [
        ("payload", lambda m: _mutate(
            m, payload=RemoteQueryPayload(query="forged", requested_capabilities=[])),
         InvalidSignatureError),  # 24
        ("sender", lambda m: _mutate(m, sender=other),
         SenderMismatchError),  # 25
        ("recipient", lambda m: _mutate(m, recipient="agent:mallory"),
         InvalidSignatureError),  # 26
        ("request_id", lambda m: _mutate(m, request_id="req_999"),
         InvalidSignatureError),  # 27
        ("message_id", lambda m: _mutate(m, message_id="msg_999"),
         InvalidSignatureError),  # 28
        ("created_at", lambda m: _mutate(m, created_at=BASE + timedelta(minutes=5)),
         InvalidSignatureError),  # 29
        ("expires_at", lambda m: _mutate(m, expires_at=BASE + timedelta(hours=2)),
         InvalidSignatureError),  # 30
        ("protocol_version", lambda m: _mutate(m, protocol_version="2.0"),
         ProtocolValidationError),  # 31
        ("type", lambda m: _mutate(
            m, type=MessageType.REMOTE_RESPONSE,
            payload=RemoteResponsePayload(status="ok", answer="forged")),
         InvalidSignatureError),  # 32
    ]


@pytest.mark.parametrize(
    "field,mutate,expected",
    [pytest.param(f, mut, exc, id=f"tamper-{f}") for f, mut, exc in _tamper_cases()],
)
def test_tampering_fails_verification(field, mutate, expected):
    identity, signed = _signed()
    with pytest.raises(expected):
        verify_message(mutate(signed), identity.public_key, now=NOW)


def test_signature_mutation_fails_verification():  # 33
    identity, signed = _signed()
    # Flip the FIRST character: it sits in a full 4-char quantum, so every
    # alphabet change alters decoded bytes. (The last character of
    # unpadded base64url carries unused trailing bits, so flipping it
    # need not change the bytes at all.)
    first = signed.signature[0]
    forged = ("A" if first != "A" else "B") + signed.signature[1:]
    assert forged != signed.signature
    with pytest.raises((MalformedSignatureError, InvalidSignatureError)):
        verify_message(
            _mutate(signed, signature=forged), identity.public_key, now=NOW
        )


def test_signer_and_verifier_share_component1_canonicalization():  # 34
    identity, signed = _signed()
    revived = message_from_dict(message_to_dict(signed))
    assert canonicalize_message(revived) == canonicalize_message(signed)
    result = verify_message(revived, identity.public_key, now=NOW)
    assert result.agent_id == identity.agent_id


def test_equivalent_timestamp_representations_verify():  # 35
    identity, signed = _signed()
    data = message_to_dict(signed)
    data["created_at"] = "2026-01-01T12:00:00Z"
    data["expires_at"] = "2026-01-01T08:00:00-05:00"  # same instant
    revived = message_from_dict(data)
    assert revived.created_at == signed.created_at
    verify_message(revived, identity.public_key, now=NOW)


def test_field_order_does_not_affect_verification():  # 36
    identity, signed = _signed()
    data = message_to_dict(signed)
    shuffled = json.dumps(dict(reversed(list(data.items()))))
    verify_message(
        message_from_json(shuffled), identity.public_key, now=NOW
    )


def test_signature_field_excluded_from_signed_bytes():  # 37
    _, signed = _signed()
    assert canonicalize_message(signed) == canonicalize_message(
        dataclasses.replace(signed, signature="")
    )
    assert '"signature"' not in canonicalize_message(signed).decode("utf-8")


def test_valid_unexpired_message_accepted():  # 43
    identity, signed = _signed()
    result = verify_message(signed, identity.public_key, now=NOW)
    assert (result.agent_id, result.message_id, result.request_id) == (
        identity.agent_id, "msg_001", "req_123",
    )


def test_expired_message_rejected():  # 44
    identity, signed = _signed()
    with pytest.raises(ExpiredMessageError):
        verify_message(signed, identity.public_key, now=LATE)


def test_created_equals_expires_boundary():  # 45
    identity = create_identity()
    data = message_to_dict(_message(identity))
    data["created_at"] = data["expires_at"] = "2026-01-01T12:00:00+00:00"
    with pytest.raises(
        ProtocolSerializationError, match="expires_at must be after"
    ):
        message_from_dict(data)


def test_timezone_normalized_timestamps_verify():  # 46
    identity = create_identity()
    plus5 = timezone(timedelta(hours=5))
    message = _message(
        identity,
        created_at=datetime(2026, 1, 1, 17, 0, tzinfo=plus5),  # 12:00 UTC
        expires_at=datetime(2026, 1, 1, 18, 0, tzinfo=plus5),
    )
    signed = sign_message(message, identity.private_key)
    result = verify_message(
        signed, identity.public_key,
        now=datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc),
    )
    assert result.agent_id == identity.agent_id


def test_malformed_signature_returns_security_error():  # 47
    identity, signed = _signed()
    with pytest.raises(MalformedSignatureError):
        verify_message(
            _mutate(signed, signature="!!!not-base64!!!"),
            identity.public_key, now=NOW,
        )
    with pytest.raises(MalformedSignatureError):
        verify_message(
            _mutate(signed, signature="aGk"),  # decodes to 2 bytes
            identity.public_key, now=NOW,
        )


def test_malformed_key_returns_security_error():  # 48
    _, signed = _signed()
    with pytest.raises(InvalidPublicKeyError):
        verify_message(signed, "not-a-key", now=NOW)  # type: ignore[arg-type]
    private_key = create_identity().private_key
    with pytest.raises(InvalidPublicKeyError):
        verify_message(signed, private_key, now=NOW)  # type: ignore[arg-type]


def test_invalid_signature_is_exact_security_error():  # 49
    identity, signed = _signed()
    forged = _mutate(
        signed,
        payload=RemoteQueryPayload(query="forged", requested_capabilities=[]),
    )
    with pytest.raises(InvalidSignatureError) as exc_info:
        verify_message(forged, identity.public_key, now=NOW)
    assert type(exc_info.value) is InvalidSignatureError


def test_verification_never_leaks_secret_material():  # 50
    identity, signed = _signed()
    raw = private_key_to_bytes(identity.private_key)
    texts = []
    for operation in (
        lambda: verify_message(
            _mutate(signed, payload=RemoteQueryPayload(query="x", requested_capabilities=[])),
            identity.public_key, now=NOW),
        lambda: verify_message(
            _mutate(signed, signature="!!!"), identity.public_key, now=NOW),
        lambda: verify_message(signed, identity.public_key, now=LATE),
        lambda: sign_message(_message(identity), "nope"),  # type: ignore[arg-type]
    ):
        try:
            operation()
        except Exception as exc:  # noqa: BLE001 - auditing leakage
            texts.append(str(exc))
    assert texts, "expected operations to fail"
    for text in texts:
        assert raw.decode("latin1") not in text
        assert raw.hex() not in text


def test_cross_key_verification_rejected():
    first, second = create_identity(), create_identity()
    signed = sign_message(_message(first), first.private_key)
    with pytest.raises(SenderMismatchError):
        verify_message(signed, second.public_key, now=NOW)


def test_padded_and_standard_base64_signatures_verify():
    from backend.identity.signing import decode_signature

    identity, signed = _signed()
    raw = decode_signature(signed.signature)
    padded = base64.urlsafe_b64encode(raw).decode("ascii")
    assert padded.endswith("=")
    standard = base64.b64encode(raw).decode("ascii")
    for spelling in (padded, standard):
        assert spelling != signed.signature  # non-canonical spellings
        result = verify_message(
            _mutate(signed, signature=spelling), identity.public_key, now=NOW
        )
        assert result.agent_id == identity.agent_id
