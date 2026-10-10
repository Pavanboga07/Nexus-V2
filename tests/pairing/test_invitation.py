"""Invitation crypto: create, validate, reject, never leak the secret."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.pairing import invitation
from backend.pairing.errors import PairingInvalidError


def _pub(identity):
    import base64

    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _now():
    return datetime.now(timezone.utc)


def test_create_validate_roundtrip():
    identity = create_identity()
    code, invitation_id, secret, expires_at = invitation.create_invitation(
        identity.agent_id, identity.private_key, _pub(identity),
        3600, _now(),
    )
    assert code.startswith("inv.")
    assert invitation_id.startswith("inv_")
    assert secret
    fields, returned_secret = invitation.split_code(code)
    assert returned_secret == secret
    assert fields["invitation_id"] == invitation_id
    assert fields["agent_id"] == identity.agent_id
    assert fields["public_key"] == _pub(identity)
    assert invitation.secret_matches(secret, invitation.secret_hash(secret))
    assert not invitation.secret_matches("wrong", invitation.secret_hash(secret))
    fingerprint = invitation.fingerprint(_pub(identity))
    assert len(fingerprint) == 19  # XXXX-XXXX-XXXX-XXXX


def test_tampered_envelope_rejected():
    identity = create_identity()
    code, _, secret, _ = invitation.create_invitation(
        identity.agent_id, identity.private_key, _pub(identity),
        3600, _now(),
    )
    head, envelope, _ = code.split(".")
    tampered = head + "." + envelope[:-4] + (
        "AAAA" if not envelope.endswith("AAAA") else "BBBB") + "." + secret
    with pytest.raises(PairingInvalidError):
        invitation.split_code(tampered)


def test_wrong_key_binding_rejected():
    issuer = create_identity()
    other = create_identity()
    code, _, _, _ = invitation.create_invitation(
        issuer.agent_id, issuer.private_key, _pub(issuer), 3600, _now())
    head, envelope, secret = code.split(".")
    import base64
    import json

    fields = json.loads(
        base64.urlsafe_b64decode(envelope + "=" * (-len(envelope) % 4)))
    fields["public_key"] = _pub(other)
    forged = head + "." + base64.urlsafe_b64encode(
        json.dumps(fields, sort_keys=True,
                   separators=(",", ":")).encode()
    ).rstrip(b"=").decode() + "." + secret
    with pytest.raises(PairingInvalidError):
        invitation.split_code(forged)


def test_expired_invitation_rejected():
    identity = create_identity()
    past = _now() - timedelta(hours=2)
    code, _, _, _ = invitation.create_invitation(
        identity.agent_id, identity.private_key, _pub(identity),
        3600, past,
    )
    with pytest.raises(PairingInvalidError, match="expired"):
        invitation.split_code(code)


def test_malformed_codes_rejected():
    for bad in ("", "inv.", "inv.a.b.c", "xxx.yyy.zzz", "inv.!!!.???",
                None, 42):
        with pytest.raises(PairingInvalidError):
            invitation.split_code(bad)


def test_ttl_bounds_rejected():
    identity = create_identity()
    for bad_ttl in (0, -5, 31 * 24 * 3600, "soon"):
        with pytest.raises(PairingInvalidError):
            invitation.create_invitation(
                identity.agent_id, identity.private_key, _pub(identity),
                bad_ttl, _now(),
            )


def test_secret_never_appears_in_errors():
    identity = create_identity()
    code, _, secret, _ = invitation.create_invitation(
        identity.agent_id, identity.private_key, _pub(identity),
        3600, _now(),
    )
    head, envelope, secret_tail = code.split(".")
    broken = head + "." + envelope[:-6] + "AAAAAA" + "." + secret_tail
    try:
        invitation.split_code(broken)
    except PairingInvalidError as exc:
        assert secret not in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected rejection")
    assert secret not in invitation.secret_hash(secret)
