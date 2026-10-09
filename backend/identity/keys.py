"""Ed25519 key primitives (generation and raw serialization only).

Uses the ``cryptography`` library's secure key generation (never
``random``, timestamps, or homemade entropy). Raw 32-byte form is the
documented serialization for both keys; richer formats (PEM) are out of
scope for Component 2. No logging or printing of secret material occurs
anywhere in this module.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import InvalidPrivateKeyError, InvalidPublicKeyError
from .models import PRIVATE_KEY_LENGTH, PUBLIC_KEY_LENGTH


def generate_private_key() -> Ed25519PrivateKey:
    """Generate a fresh Ed25519 private key from OS-provided entropy."""
    return Ed25519PrivateKey.generate()


def private_key_to_bytes(private_key: Ed25519PrivateKey) -> bytes:
    """Serialize a private key to raw 32 bytes (keep secret)."""
    if not isinstance(private_key, Ed25519PrivateKey):
        raise InvalidPrivateKeyError(
            "private key must be an Ed25519PrivateKey"
        )
    return private_key.private_bytes_raw()


def private_key_from_bytes(data: bytes | bytearray) -> Ed25519PrivateKey:
    """Load a private key from raw bytes (rejects malformed input)."""
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise InvalidPrivateKeyError("private key material is empty")
    if len(data) != PRIVATE_KEY_LENGTH:
        raise InvalidPrivateKeyError(
            "private key must be "
            f"{PRIVATE_KEY_LENGTH} bytes, got {len(data)}"
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(bytes(data))
    except Exception as exc:
        raise InvalidPrivateKeyError(
            "malformed Ed25519 private key"
        ) from exc


def public_key_to_bytes(public_key: Ed25519PublicKey) -> bytes:
    """Serialize a public key to raw 32 bytes (safe to distribute)."""
    if not isinstance(public_key, Ed25519PublicKey):
        raise InvalidPublicKeyError(
            "public key must be an Ed25519PublicKey"
        )
    return public_key.public_bytes_raw()


def public_key_from_bytes(data: bytes | bytearray) -> Ed25519PublicKey:
    """Load a public key from raw bytes (rejects malformed input).

    Raw form carries no type tag, so a 32-byte key of another scheme is
    indistinguishable here; callers holding typed key objects are
    protected by the isinstance checks above and in the signing API.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise InvalidPublicKeyError("public key material is empty")
    if len(data) != PUBLIC_KEY_LENGTH:
        raise InvalidPublicKeyError(
            "public key must be "
            f"{PUBLIC_KEY_LENGTH} bytes, got {len(data)}"
        )
    try:
        return Ed25519PublicKey.from_public_bytes(bytes(data))
    except Exception as exc:
        raise InvalidPublicKeyError(
            "malformed Ed25519 public key"
        ) from exc
