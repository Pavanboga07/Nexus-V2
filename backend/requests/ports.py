"""Inbound ports: capabilities the service needs but must not own.

The service never holds private keys and never keeps a global mutable
identity: callers inject an :class:`IdentityProvider` (tests use an
in-memory dict; production will back it with a key store).
"""

from __future__ import annotations

from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)


class IdentityProvider(Protocol):
    """Resolves the signing key owned by one agent id."""

    def private_key_for(self, agent_id: str) -> Ed25519PrivateKey:
        """Return the private key for ``agent_id`` (KeyError if unknown)."""
        ...  # pragma: no cover - interface only
