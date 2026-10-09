"""Message verification: authenticity + security validity window.

Check order (documented rationale inline):

1. Protocol structure — reject malformed messages before spending any
   trust reasoning on them; naive ``now`` is rejected by Component 1.
2. Sender/key consistency — a valid signature under the wrong key must
   never read as the claimed sender's attestation (``valid signature !=
   valid claimed identity``), so identity binding precedes crypto.
3. Expiry — stale messages are rejected even if authentic.
4. Replay membership — duplicates are rejected before verification so a
   replayed-but-valid message can never be re-accepted; the id is only
   RECORDED after signature success, so forged messages cannot pollute
   the registry and block legitimate traffic.
5. Signature verification — Ed25519 over Component 1 canonical bytes.

Verification answers "authentic and within window?" only. It never
decides capability authorization, never executes payloads, and never
uses unverified content for trust decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cryptography.exceptions import InvalidSignature as CryptoInvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

from backend.protocol.canonicalization import canonicalize_message
from backend.protocol.models import NexusMessage
from backend.protocol.validation import (
    ProtocolValidationError,
    is_expired,
    validate_message,
)

from .errors import (
    ExpiredMessageError,
    InvalidPublicKeyError,
    InvalidSignatureError,
    ReplayDetectedError,
    SenderMismatchError,
)
from .identity import verify_agent_identity
from .replay import ReplayRegistry
from .signing import decode_signature


@dataclass(frozen=True)
class VerificationResult:
    """Success record for an authenticated, in-window message."""

    agent_id: str
    message_id: str
    request_id: str


def verify_message(
    message: NexusMessage,
    public_key: Ed25519PublicKey,
    *,
    now: Optional[datetime] = None,
    replay_registry: ReplayRegistry | None = None,
) -> VerificationResult:
    """Verify authenticity and validity window; return result or raise.

    Raises :class:`ProtocolValidationError` for structurally invalid
    messages and the dedicated ``*Error`` types from :mod:`.errors`
    for security failures. Raw ``cryptography`` exceptions never escape
    (``InvalidSignature`` becomes :class:`InvalidSignatureError`).
    """
    if not isinstance(message, NexusMessage):
        raise ProtocolValidationError(["message must be a NexusMessage"])
    if not isinstance(public_key, Ed25519PublicKey):
        raise InvalidPublicKeyError(
            "public key must be an Ed25519PublicKey"
        )
    errors = validate_message(message, check_expiry=False, now=now)
    if errors:
        raise ProtocolValidationError(errors)
    if not verify_agent_identity(message.sender, public_key):
        raise SenderMismatchError(
            f"message sender {message.sender!r} does not match "
            "the supplied public key"
        )
    if is_expired(message, now=now):
        raise ExpiredMessageError(
            f"message {message.message_id!r} has expired"
        )
    # One clock for the whole call: membership expiry must agree with the
    # expiry decision above (naive ``now`` already rejected by Component 1).
    effective_now = now if now is not None else datetime.now(timezone.utc)
    if replay_registry is not None and replay_registry.has_seen(
        message.message_id, effective_now
    ):
        raise ReplayDetectedError(
            f"message {message.message_id!r} was already seen"
        )
    signing_bytes = canonicalize_message(message)
    signature = decode_signature(message.signature)
    try:
        public_key.verify(signature, signing_bytes)
    except CryptoInvalidSignature as exc:
        raise InvalidSignatureError(
            f"invalid signature for message {message.message_id!r}"
        ) from exc
    if replay_registry is not None:
        replay_registry.record(message.message_id, message.expires_at)
    return VerificationResult(
        agent_id=message.sender,
        message_id=message.message_id,
        request_id=message.request_id,
    )
