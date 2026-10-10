"""Provider credential crypto: roundtrip, wrong-key and missing-key close."""

import pytest
from cryptography.fernet import Fernet

from backend.provider_config import crypto
from backend.provider_config.errors import (
    CredentialStoreUnavailable,
    ProviderConfigInvalid,
)


def _master():
    return Fernet.generate_key().decode("ascii")


def test_encrypt_decrypt_roundtrip():
    master = _master()
    token = crypto.encrypt_key(master, "sk-test-secret")
    assert token != b"sk-test-secret"
    assert b"sk-test-secret" not in token
    assert crypto.decrypt_key(master, token) == "sk-test-secret"


def test_missing_master_key_fails_closed():
    with pytest.raises(CredentialStoreUnavailable):
        crypto.encrypt_key(None, "sk-test-secret")
    with pytest.raises(CredentialStoreUnavailable):
        crypto.decrypt_key("", b"whatever")


def test_invalid_master_key_format_rejected():
    with pytest.raises(ProviderConfigInvalid):
        crypto.encrypt_key("not-a-fernet-key", "sk-test-secret")


def test_wrong_master_key_cannot_decrypt():
    token = crypto.encrypt_key(_master(), "sk-test-secret")
    with pytest.raises(CredentialStoreUnavailable):
        crypto.decrypt_key(_master(), token)
