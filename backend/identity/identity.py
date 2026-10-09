"""Agent identity: deterministic ids, public documents, consistency checks.

The agent id is ``agent_`` + lowercase hex SHA-256 over the raw 32-byte
Ed25519 public key. It depends only on the public key: no usernames, no
database state, no mutable metadata — so reloading the same key always
yields the same id, and different keys yield different ids.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import keys
from .errors import (
    InvalidIdentityError,
    InvalidPrivateKeyError,
    InvalidPublicKeyError,
)
from .models import (
    AGENT_ID_PREFIX,
    KEY_ALGORITHM,
    AgentIdentity,
    PublicIdentity,
)


def derive_agent_id(public_key: Ed25519PublicKey) -> str:
    """Derive the deterministic ``agent_<sha256-hex>`` id for a public key."""
    if not isinstance(public_key, Ed25519PublicKey):
        raise InvalidPublicKeyError(
            "public key must be an Ed25519PublicKey"
        )
    digest = hashlib.sha256(keys.public_key_to_bytes(public_key)).hexdigest()
    return f"{AGENT_ID_PREFIX}{digest}"


def verify_agent_identity(agent_id: Any, public_key: Any) -> bool:
    """Return True iff ``public_key`` deterministically yields ``agent_id``.

    Total predicate: never raises, so verification can use it for the
    sender/key consistency check before touching signatures. A valid
    signature under a mismatched key still fails (see ``SenderMismatch``).
    """
    if not isinstance(agent_id, str) or not agent_id:
        return False
    try:
        return derive_agent_id(public_key) == agent_id
    except InvalidPublicKeyError:
        return False


def create_identity() -> AgentIdentity:
    """Generate a fresh agent identity (private key + derived id)."""
    private_key = keys.generate_private_key()
    return AgentIdentity(
        agent_id=derive_agent_id(private_key.public_key()),
        private_key=private_key,
    )


def identity_from_private_bytes(data: bytes | bytearray) -> AgentIdentity:
    """Reload the identity owned by serialized private key material.

    The public key and agent id are re-derived, so the result is
    internally consistent by construction and stable across reloads.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise InvalidPrivateKeyError("private key must be bytes")
    private_key = keys.private_key_from_bytes(data)
    return AgentIdentity(
        agent_id=derive_agent_id(private_key.public_key()),
        private_key=private_key,
    )


def identity_from_private_key(
    private_key: Ed25519PrivateKey,
) -> AgentIdentity:
    """Wrap a private key object into its consistent agent identity."""
    if not isinstance(private_key, Ed25519PrivateKey):
        raise InvalidPrivateKeyError(
            "private key must be an Ed25519PrivateKey"
        )
    return AgentIdentity(
        agent_id=derive_agent_id(private_key.public_key()),
        private_key=private_key,
    )


def public_identity_from_dict(data: Any) -> PublicIdentity:
    """Parse a distributed public identity document (never has secrets)."""
    if not isinstance(data, dict):
        raise InvalidIdentityError("public identity must be an object")
    agent_id = data.get("agent_id")
    algorithm = data.get("algorithm", KEY_ALGORITHM)
    encoded = data.get("public_key")
    if not isinstance(agent_id, str) or not agent_id:
        raise InvalidIdentityError("public identity needs an agent_id")
    if algorithm != KEY_ALGORITHM:
        raise InvalidIdentityError(
            f"unsupported key algorithm: {algorithm!r}"
        )
    if not isinstance(encoded, str) or not encoded:
        raise InvalidIdentityError("public identity needs a public_key")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InvalidPublicKeyError(
            "public identity key is not valid base64url"
        ) from exc
    public_key = keys.public_key_from_bytes(raw)
    if not verify_agent_identity(agent_id, public_key):
        raise InvalidIdentityError(
            "public identity agent_id does not match its public key"
        )
    return PublicIdentity(
        agent_id=agent_id, public_key=raw, algorithm=KEY_ALGORITHM
    )
