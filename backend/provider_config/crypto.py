"""Encrypted-at-rest provider keys (Fernet, explicit master key).

The master key is deployment configuration (``NEXUS_API_CREDENTIAL_KEY``,
a Fernet urlsafe-b64 key), never derived, never defaulted, and never
stored alongside the ciphertext. Without it every credential operation
fails closed — there is deliberately no plaintext fallback path, so no
code here can write or read an unencrypted key.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .errors import CredentialStoreUnavailable, ProviderConfigInvalid


def _fernet(master_key: str | None) -> Fernet:
    if not master_key:
        raise CredentialStoreUnavailable(
            "no credential master key is configured"
        )
    try:
        return Fernet(master_key.strip().encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ProviderConfigInvalid(
            "the credential master key is not a valid Fernet key"
        ) from exc


def encrypt_key(master_key: str | None, api_key: str) -> bytes:
    """Encrypt one provider key; fails closed without a valid master."""
    return _fernet(master_key).encrypt(api_key.encode("utf-8"))


def decrypt_key(master_key: str | None, token: bytes) -> str:
    """Decrypt one provider key; wrong-key ciphertext fails closed."""
    try:
        return _fernet(master_key).decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialStoreUnavailable(
            "stored credentials cannot be decrypted with this master key"
        ) from exc
