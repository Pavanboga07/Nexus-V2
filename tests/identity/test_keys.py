"""Key generation and serialization tests."""

import re

import pytest
from cryptography.hazmat.primitives import serialization as crypto_ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.rsa import (
    generate_private_key as generate_rsa_private_key,
)

from backend.identity.errors import (
    InvalidPrivateKeyError,
    InvalidPublicKeyError,
)
from backend.identity.identity import (
    create_identity,
    derive_agent_id,
    identity_from_private_bytes,
)
from backend.identity.keys import (
    generate_private_key,
    private_key_from_bytes,
    private_key_to_bytes,
    public_key_from_bytes,
    public_key_to_bytes,
)

AGENT_ID_RE = re.compile(r"^agent_[0-9a-f]{64}$")


def test_generated_identity_has_private_key():  # 1
    identity = create_identity()
    assert isinstance(identity.private_key, Ed25519PrivateKey)


def test_generated_identity_has_public_key():  # 2
    identity = create_identity()
    assert isinstance(identity.public_key, Ed25519PublicKey)
    assert len(public_key_to_bytes(identity.public_key)) == 32


def test_public_key_is_derivable():  # 3
    private_key = generate_private_key()
    derived = private_key.public_key()
    assert isinstance(derived, Ed25519PublicKey)
    assert public_key_to_bytes(
        public_key_from_bytes(public_key_to_bytes(derived))
    ) == public_key_to_bytes(derived)


def test_agent_id_is_deterministic():  # 4
    identity = create_identity()
    assert derive_agent_id(identity.public_key) == identity.agent_id
    assert AGENT_ID_RE.match(identity.agent_id)


def test_same_key_reload_gives_same_agent_id():  # 5
    identity = create_identity()
    raw = private_key_to_bytes(identity.private_key)
    reloaded = identity_from_private_bytes(raw)
    assert reloaded.agent_id == identity.agent_id
    assert public_key_to_bytes(reloaded.public_key) == public_key_to_bytes(
        identity.public_key
    )


def test_different_keys_produce_different_agent_ids():  # 6
    ids = {create_identity().agent_id for _ in range(5)}
    assert len(ids) == 5


def test_private_key_never_in_public_identity():  # 7
    identity = create_identity()
    public = identity.public_identity()
    assert not hasattr(public, "private_key")
    assert set(public.to_dict()) == {"agent_id", "algorithm", "public_key"}
    raw = private_key_to_bytes(identity.private_key)
    assert raw not in repr(public).encode()
    assert raw.decode("latin1") not in str(public.to_dict())


def test_private_key_serialization_round_trip():  # 8
    private_key = generate_private_key()
    restored = private_key_from_bytes(private_key_to_bytes(private_key))
    assert private_key_to_bytes(restored) == private_key_to_bytes(
        private_key
    )


def test_public_key_serialization_round_trip():  # 9
    public_key = generate_private_key().public_key()
    restored = public_key_from_bytes(public_key_to_bytes(public_key))
    assert public_key_to_bytes(restored) == public_key_to_bytes(public_key)


@pytest.mark.parametrize(
    "bad", [b"", b"short", b"x" * 31, b"x" * 33, "not-bytes", None, 123]
)
def test_malformed_private_key_rejected(bad):  # 10
    with pytest.raises(InvalidPrivateKeyError):
        private_key_from_bytes(bad)


@pytest.mark.parametrize(
    "bad", [b"", b"short", b"x" * 31, b"x" * 33, b"x" * 64, "not-bytes"]
)
def test_malformed_public_key_rejected(bad):  # 11
    with pytest.raises(InvalidPublicKeyError):
        public_key_from_bytes(bad)


def test_wrong_key_type_rejected():  # 12
    rsa_private = generate_rsa_private_key(65537, 1024)
    with pytest.raises(InvalidPrivateKeyError):
        private_key_to_bytes(rsa_private.public_key())  # type: ignore[arg-type]
    rsa_der = rsa_private.public_key().public_bytes(
        crypto_ser.Encoding.DER, crypto_ser.PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(InvalidPublicKeyError):
        public_key_from_bytes(rsa_der)
    with pytest.raises(InvalidPublicKeyError):
        public_key_to_bytes(rsa_private)  # type: ignore[arg-type]


@pytest.mark.parametrize("length", [0, 1, 31, 33, 64])
def test_wrong_key_length_rejected(length):  # 13
    with pytest.raises(InvalidPrivateKeyError):
        private_key_from_bytes(b"k" * length or b"")
    with pytest.raises(InvalidPublicKeyError):
        public_key_from_bytes(b"k" * length or b"")
