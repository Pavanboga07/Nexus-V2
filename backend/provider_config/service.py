"""Owner-scoped provider configuration (validation, storage, use).

Resolution order for execution is: saved database configuration first
(decrypted with the deployment master key), then the existing
environment-based runtime. Saving never returns the key; reading never
includes it; errors never echo it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage

from backend.persistence.database import session_scope
from backend.persistence.repositories.pairing import (
    ProviderCredentialRepository,
)

from . import crypto
from .errors import (
    CredentialNotConfigured,
    ProviderAuthError,
    ProviderCallError,
    ProviderConfigInvalid,
    ProviderUnavailableError,
)

#: Providers the settings UI may offer. Keys are the ``provider`` half
#: of ``NEXUS_LLM_MODEL``; values are human labels. google_genai is
#: first because its integration is installed and verified.
SUPPORTED_PROVIDERS: dict[str, str] = {
    "google_genai": "Google Gemini",
}

_TEST_PROMPT = "Reply with exactly: OK"
_TEST_TIMEOUT_S = 20.0
_MAX_KEY_CHARS = 8192


def _check_text(name: str, value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigInvalid(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise ProviderConfigInvalid(
            f"{name} must be at most {limit} characters")
    return value.strip()


def validate_candidate(provider: Any, model: Any, api_key: Any) -> None:
    """Validate a candidate configuration without touching storage."""
    if provider not in SUPPORTED_PROVIDERS:
        raise ProviderConfigInvalid(
            "provider must be one of: "
            + ", ".join(sorted(SUPPORTED_PROVIDERS))
        )
    _check_text("model", model, 128)
    _check_text("api_key", api_key, _MAX_KEY_CHARS)


def build_provider_model(provider: str, model: str, api_key: str):
    """Construct the LangChain model for verified inputs (no I/O)."""
    if provider == "google_genai":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:
            raise ProviderUnavailableError(
                "the Google provider integration is not installed"
            ) from exc
        return ChatGoogleGenerativeAI(model=model, google_api_key=api_key)
    raise ProviderConfigInvalid(
        "provider must be one of: " + ", ".join(sorted(SUPPORTED_PROVIDERS))
    )


def _safe_detail(exc: BaseException, api_key: str) -> str:
    """One-line failure detail that can never carry the secret."""
    name = type(exc).__name__
    text = str(exc) or name
    if api_key:
        text = text.replace(api_key, "[redacted]")
    text = " ".join(text.split())
    return text[:300] if len(text) > 300 else text


def _classify_test_failure(exc: BaseException, api_key: str) -> Exception:
    name = type(exc).__name__.lower()
    detail = _safe_detail(exc, api_key)
    blob = f"{name} {detail}".lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ProviderUnavailableError(
            "the provider did not answer in time")
    if any(word in blob for word in (
        "auth", "permission", "credential", "api key", "apikey",
        "unauthorized", "forbidden", "401", "403",
    )):
        return ProviderAuthError("the provider rejected the credentials")
    if any(word in blob for word in (
        "connect", "network", "dns", "resolve", "unreachable",
        "timeout", "timed out", "connection",
    )):
        return ProviderUnavailableError(
            "the provider could not be reached")
    return ProviderCallError(
        f"the provider call failed ({type(exc).__name__})")


class ProviderConfigService:
    """Save, read (keyless), delete, and use one owner's configuration."""

    def __init__(self, sessions: Any, credential_key: str | None) -> None:
        self._sessions = sessions
        self._credential_key = credential_key

    async def get_public(self, owner: UUID) -> dict[str, Any]:
        """Configured flag plus non-secret metadata (never the key)."""
        async with session_scope(self._sessions) as session:
            row = await ProviderCredentialRepository(session).get(owner)
        if row is None:
            return {"configured": False}
        return {
            "configured": True,
            "provider": row.provider,
            "model": row.model,
            "updated_at": row.updated_at.isoformat(),
        }

    async def save(
        self, owner: UUID, provider: Any, model: Any, api_key: Any,
    ) -> dict[str, Any]:
        """Validate, encrypt, and store one owner's configuration."""
        validate_candidate(provider, model, api_key)
        token = crypto.encrypt_key(self._credential_key, api_key.strip())
        async with session_scope(self._sessions) as session:
            await ProviderCredentialRepository(session).upsert(
                owner, provider, model.strip(), token)
        return await self.get_public(owner)

    async def delete(self, owner: UUID) -> bool:
        """Remove one owner's configuration; True when one existed."""
        async with session_scope(self._sessions) as session:
            return await ProviderCredentialRepository(session).delete(owner)

    async def load_credentials(
        self, owner: UUID
    ) -> tuple[str, str, str] | None:
        """Return ``(provider, model, api_key)`` or None when absent."""
        async with session_scope(self._sessions) as session:
            row = await ProviderCredentialRepository(session).get(owner)
        if row is None:
            return None
        api_key = crypto.decrypt_key(
            self._credential_key, row.key_encrypted)
        return row.provider, row.model, api_key

    async def test_saved(self, owner: UUID) -> dict[str, Any]:
        """Run a minimal live call with the saved configuration."""
        credentials = await self.load_credentials(owner)
        if credentials is None:
            raise CredentialNotConfigured(
                "no provider configuration is saved for this owner")
        provider, model, api_key = credentials
        return await self.test_candidate(provider, model, api_key)

    async def test_candidate(
        self, provider: Any, model: Any, api_key: Any
    ) -> dict[str, Any]:
        """Run a minimal live call with explicit values (not stored)."""
        validate_candidate(provider, model, api_key)
        chat_model = build_provider_model(
            provider, model.strip(), api_key.strip())
        started = asyncio.get_running_loop().time()
        try:
            reply = await asyncio.wait_for(
                chat_model.ainvoke([HumanMessage(content=_TEST_PROMPT)]),
                timeout=_TEST_TIMEOUT_S,
            )
        except Exception as exc:
            raise _classify_test_failure(exc, api_key.strip()) from exc
        elapsed_ms = int(
            (asyncio.get_running_loop().time() - started) * 1000)
        content = getattr(reply, "content", "")
        text = content if isinstance(content, str) else str(content)
        return {
            "ok": True,
            "provider": provider,
            "model": model.strip(),
            "latency_ms": elapsed_ms,
            "reply_preview": " ".join(text.split())[:200],
        }
