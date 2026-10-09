"""Component 2 error model.

Every exception below is safe to surface to callers and logs: messages
contain only non-sensitive identifiers (agent ids, message ids, key
lengths, algorithm names). Private key material, secret bytes, and
filesystem paths are never included in error text.
"""

from __future__ import annotations


class IdentityError(Exception):
    """Base class for all Component 2 security errors."""


class InvalidIdentityError(IdentityError):
    """An agent identity document or derivation is internally inconsistent."""


class InvalidPrivateKeyError(IdentityError):
    """Private key material is missing, malformed, or of the wrong type."""


class InvalidPublicKeyError(IdentityError):
    """A public key is missing, malformed, or of the wrong type."""


class MalformedSignatureError(IdentityError):
    """A protocol signature cannot be decoded (bad encoding or length)."""


class InvalidSignatureError(IdentityError):
    """A decodable signature does not authenticate the message."""


class ExpiredMessageError(IdentityError):
    """A message is outside its security validity window."""


class ReplayDetectedError(IdentityError):
    """A message_id has already been seen within the replay window."""


class SenderMismatchError(IdentityError):
    """The claimed sender does not match the signing/verifying identity."""
