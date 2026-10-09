"""Message signing over Component 1 canonical bytes.

Flow: validate structure (signature presence ignored — signing runs
pre-signing) → bind sender to the signing key → canonicalize (which
excludes ``signature``) → Ed25519-sign → return a NEW message carrying
the base64url signature. The input message is never mutated.

Signature wire encoding: raw 64-byte Ed25519 signature → base64url
(URL-safe alphabet, ``=`` padding stripped for compactness) → protocol
``signature`` string. Decoding restores padding before verifying length.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from backend.protocol.models import NexusMessage
from backend.protocol.canonicalization import canonicalize_message
from backend.protocol.validation import ProtocolValidationError, validate_message

from .errors import (
    InvalidPrivateKeyError,
    MalformedSignatureError,
    SenderMismatchError,
)
from .identity import derive_agent_id

#: Raw Ed25519 signature length in bytes.
SIGNATURE_LENGTH = 64


def encode_signature(raw: bytes | bytearray) -> str:
    """Encode 64 raw signature bytes as unpadded base64url text."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != SIGNATURE_LENGTH:
        raise MalformedSignatureError(
            "signature must be "
            f"{SIGNATURE_LENGTH} raw Ed25519 signature bytes"
        )
    return (
        base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")
    )


def decode_signature(text: str) -> bytes:
    """Decode a protocol signature string back to 64 raw bytes."""
    if not isinstance(text, str) or not text:
        raise MalformedSignatureError("signature must be a non-empty string")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise MalformedSignatureError(
            "signature is not valid base64url"
        ) from exc
    if len(raw) != SIGNATURE_LENGTH:
        raise MalformedSignatureError(
            "signature must decode to "
            f"{SIGNATURE_LENGTH} bytes, got {len(raw)}"
        )
    return raw


def sign_message(
    message: NexusMessage, private_key: Ed25519PrivateKey
) -> NexusMessage:
    """Sign a Nexus protocol message; return a new signed message.

    The message's ``sender`` must equal the agent id derived from
    ``private_key`` — signing never attests another agent's identity.
    Any pre-existing ``signature`` value is replaced (harmless: the
    field is excluded from the signed bytes, so an unchanged message
    re-signs to the identical signature — Ed25519 is deterministic).
    Expiry is intentionally NOT enforced here; acceptance decisions
    belong to verification.
    """
    if not isinstance(message, NexusMessage):
        raise TypeError("message must be a NexusMessage")
    if not isinstance(private_key, Ed25519PrivateKey):
        raise InvalidPrivateKeyError(
            "private key must be an Ed25519PrivateKey"
        )
    # Structural validity ignoring signature presence: validate a copy
    # carrying a placeholder so every other rule applies unchanged.
    candidate = dataclasses.replace(message, signature="nexus-presigning")
    errors = validate_message(candidate, check_expiry=False)
    if errors:
        raise ProtocolValidationError(errors)
    expected_sender = derive_agent_id(private_key.public_key())
    if message.sender != expected_sender:
        raise SenderMismatchError(
            f"message sender {message.sender!r} does not match "
            f"signing identity {expected_sender!r}"
        )
    signing_bytes = canonicalize_message(message)
    raw_signature = private_key.sign(signing_bytes)
    return dataclasses.replace(
        message, signature=encode_signature(raw_signature)
    )
