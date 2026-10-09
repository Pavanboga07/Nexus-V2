"""Agent identity derivation and public-document tests."""

from backend.identity.errors import (
    InvalidIdentityError,
    InvalidPublicKeyError,
)
from backend.identity.identity import (
    create_identity,
    public_identity_from_dict,
    verify_agent_identity,
)


def test_correct_public_key_matches_agent_id():  # 14
    identity = create_identity()
    assert verify_agent_identity(identity.agent_id, identity.public_key)


def test_wrong_public_key_does_not_match_agent_id():  # 15
    first, second = create_identity(), create_identity()
    assert not verify_agent_identity(first.agent_id, second.public_key)


def test_mutated_agent_id_rejected():  # 16
    identity = create_identity()
    mutated = identity.agent_id[:-1] + (
        "0" if identity.agent_id[-1] != "0" else "1"
    )
    assert mutated != identity.agent_id
    assert not verify_agent_identity(mutated, identity.public_key)
    assert not verify_agent_identity("", identity.public_key)
    assert not verify_agent_identity(None, identity.public_key)
    assert not verify_agent_identity(identity.agent_id, "not-a-key")


def test_public_identity_document_round_trip():
    identity = create_identity()
    public = identity.public_identity()
    assert public.agent_id == identity.agent_id
    assert public.algorithm == "Ed25519"
    restored = public_identity_from_dict(public.to_dict())
    assert restored == public


def test_public_identity_with_mismatched_id_rejected():
    first, second = create_identity(), create_identity()
    document = second.public_identity().to_dict()
    document["agent_id"] = first.agent_id
    try:
        public_identity_from_dict(document)
    except InvalidIdentityError:
        return
    raise AssertionError("mismatched public identity was accepted")


def test_public_identity_with_bad_algorithm_rejected():
    identity = create_identity()
    document = identity.public_identity().to_dict()
    document["algorithm"] = "RSA"
    try:
        public_identity_from_dict(document)
    except InvalidIdentityError:
        return
    raise AssertionError("unsupported algorithm was accepted")


def test_derive_agent_id_rejects_wrong_key_object():
    import pytest

    with pytest.raises(InvalidPublicKeyError):
        from backend.identity.identity import derive_agent_id

        derive_agent_id(object())  # type: ignore[arg-type]
