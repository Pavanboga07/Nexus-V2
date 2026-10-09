"""Component 2 identity models (data only, no behavior).

``AgentIdentity`` is the secret-bearing local identity: it always pairs an
``agent_id`` with its Ed25519 private key and never leaves the owning
agent. ``PublicIdentity`` is the distributable counterpart: agent id plus
raw public key bytes plus algorithm metadata, and never any secret.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: Signature scheme used by every identity in this component.
KEY_ALGORITHM = "Ed25519"

#: Prefix for deterministic agent identifiers derived from public keys.
AGENT_ID_PREFIX = "agent_"

#: Raw Ed25519 key lengths in bytes.
PRIVATE_KEY_LENGTH = 32
PUBLIC_KEY_LENGTH = 32


@dataclass(frozen=True)
class AgentIdentity:
    """An agent's cryptographic identity (secret: keep local)."""

    agent_id: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)

    @property
    def public_key(self) -> Ed25519PublicKey:
        """The public key derived from this identity's private key."""
        return self.private_key.public_key()

    def public_identity(self) -> PublicIdentity:
        """Return the distributable public counterpart (no secrets)."""
        raw = self.public_key.public_bytes_raw()
        return PublicIdentity(agent_id=self.agent_id, public_key=raw)


@dataclass(frozen=True)
class PublicIdentity:
    """Distributable agent identity: safe for peers and the Gateway."""

    agent_id: str
    public_key: bytes
    algorithm: str = KEY_ALGORITHM

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain JSON-compatible data (no secrets inside)."""
        return {
            "agent_id": self.agent_id,
            "algorithm": self.algorithm,
            "public_key": base64.urlsafe_b64encode(self.public_key)
            .rstrip(b"=")
            .decode("ascii"),
        }
