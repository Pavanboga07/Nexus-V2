"""Pairing invitations: signed, single-use, expiring bootstrap trust.

An invitation lets User B authorize first contact out-of-band. It is
one opaque string with two dot-separated parts:

``inv.<base64url(JSON envelope)>.<base64url(secret)>``

The envelope carries ``invitation_id``, issuer ``agent_id``,
issuer ``public_key`` (base64url raw bytes), and ``expires_at`` (UTC
ISO-8601), plus an Ed25519 ``signature`` over the canonical JSON of
those fields. The secret is 32 random bytes; only its SHA-256 is ever
stored or compared. No private keys, provider keys, or other secrets
appear in an invitation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature

from backend.identity.identity import derive_agent_id

from .errors import PairingInvalidError

PAIR_CAPABILITY = "nexus.pair"
_INVITATION_TTL_LIMIT_S = 30 * 24 * 3600


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise PairingInvalidError("invitation is malformed")
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, binascii.Error) as exc:
        raise PairingInvalidError("invitation is malformed") from exc


def _canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def decode_public_key(value: str):
    """Decode base64url Ed25519 material or raise PairingInvalidError."""
    from backend.identity.keys import public_key_from_bytes

    raw = _b64url_decode(value)
    try:
        return public_key_from_bytes(raw), raw
    except Exception as exc:  # noqa: BLE001 - boundary translation
        raise PairingInvalidError(
            "public key is not valid Ed25519 material") from exc


def fingerprint(public_key_b64: str) -> str:
    """Short grouped fingerprint of a base64url public key."""
    _, raw = decode_public_key(public_key_b64)
    digest = hashlib.sha256(raw).hexdigest().upper()
    return "-".join(digest[index:index + 4] for index in range(0, 16, 4))


def create_invitation(
    issuer_agent_id: str,
    issuer_private_key: Any,
    issuer_public_key_b64: str,
    ttl_seconds: int,
    now: datetime,
) -> tuple[str, str, str, datetime]:
    """Build one invitation; return ``(code, invitation_id, secret, exp)``.

    The secret is returned only here — callers show it once and store
    only its hash. TTL is clamped to (0, 30 days].
    """
    if not issuer_agent_id or not issuer_public_key_b64:
        raise PairingInvalidError("issuer identity is incomplete")
    try:
        seconds = int(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise PairingInvalidError(
            "invitation lifetime must be a number of seconds") from exc
    if not 0 < seconds <= _INVITATION_TTL_LIMIT_S:
        raise PairingInvalidError(
            "invitation lifetime must be within (0, 30 days]")
    public_key, _raw = decode_public_key(issuer_public_key_b64)
    if derive_agent_id(public_key) != issuer_agent_id:
        raise PairingInvalidError(
            "issuer key does not bind to the issuer agent id")
    if now.tzinfo is None:
        raise PairingInvalidError("invitation clock must be timezone-aware")
    expires_at = now + timedelta(seconds=seconds)
    fields = {
        "v": 1,
        "invitation_id": f"inv_{uuid.uuid4().hex}",
        "agent_id": issuer_agent_id,
        "public_key": issuer_public_key_b64,
        "expires_at": expires_at.isoformat(),
    }
    signature = issuer_private_key.sign(_canonical(fields))
    fields["signature"] = _b64url_encode(signature)
    envelope = _b64url_encode(_canonical(fields))
    secret = _b64url_encode(secrets.token_bytes(32))
    return f"inv.{envelope}.{secret}", fields["invitation_id"], secret, expires_at


def split_code(code: Any) -> tuple[dict[str, Any], str]:
    """Parse and cryptographically validate an invitation code.

    Returns ``(fields, secret)``. Raises :class:`PairingInvalidError`
    for malformed, badly signed, unbound, or expired material. Never
    includes the secret in any error message.
    """
    if not isinstance(code, str):
        raise PairingInvalidError("invitation is malformed")
    parts = code.split(".")
    if len(parts) != 3 or parts[0] != "inv":
        raise PairingInvalidError("invitation is malformed")
    try:
        fields = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PairingInvalidError("invitation is malformed") from exc
    if not isinstance(fields, dict):
        raise PairingInvalidError("invitation is malformed")
    if fields.get("v") != 1:
        raise PairingInvalidError("invitation version is not supported")
    for name in ("invitation_id", "agent_id", "public_key",
                 "expires_at", "signature"):
        if not isinstance(fields.get(name), str) or not fields[name]:
            raise PairingInvalidError("invitation is malformed")
    public_key, _raw = decode_public_key(fields["public_key"])
    if derive_agent_id(public_key) != fields["agent_id"]:
        raise PairingInvalidError(
            "invitation key does not bind to its agent id")
    try:
        signature = _b64url_decode(fields["signature"])
        unsigned = {k: v for k, v in fields.items() if k != "signature"}
        public_key.verify(signature, _canonical(unsigned))
    except InvalidSignature as exc:
        raise PairingInvalidError(
            "invitation signature is invalid") from exc
    try:
        expires_at = datetime.fromisoformat(fields["expires_at"])
    except ValueError as exc:
        raise PairingInvalidError("invitation expiry is malformed") from exc
    if expires_at.tzinfo is None:
        raise PairingInvalidError("invitation expiry is malformed")
    now = datetime.now(timezone.utc)
    if now >= expires_at:
        raise PairingInvalidError("invitation has expired")
    secret = parts[2]
    if not secret:
        raise PairingInvalidError("invitation is malformed")
    return fields, secret


def secret_hash(secret: str) -> str:
    """SHA-256 hex of an invitation secret (what the database keeps)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secret_matches(secret: str, expected_hash: str) -> bool:
    """Constant-time secret comparison against the stored hash."""
    if not isinstance(secret, str) or not secret:
        return False
    return hmac.compare_digest(secret_hash(secret), expected_hash)
