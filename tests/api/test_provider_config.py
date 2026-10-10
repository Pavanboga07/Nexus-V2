"""Provider configuration routes: save/get/delete/test, keyless I/O.

Uses real PostgreSQL and the real app. The live-provider failure path
uses a bogus key over the real network (no valid credential needed);
the success path uses an injected stub at the model boundary only.
"""

import pytest
from cryptography.fernet import Fernet

from backend.provider_config.service import SUPPORTED_PROVIDERS

from .conftest import (  # noqa: F401
    OWNER,
    TOKEN,
    auth_headers,
    db,
    ensure_user,
    make_client,
    make_settings,
    make_state,
)

BOGUS_KEY = "test-invalid-key-not-real-0123456789"


async def _owner_row(db):
    from .conftest import OWNER, ensure_user

    await ensure_user(db, OWNER)


def _master():
    return Fernet.generate_key().decode("ascii")


@pytest.mark.asyncio
async def test_status_unconfigured_initially(db):
    from backend.api.app import create_app

    settings = make_settings(credential_key=_master())
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        response = await client.get(
            "/api/v1/settings/ai-provider", headers=auth_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is False
    assert body["provider"] is None
    assert body["model"] is None


@pytest.mark.asyncio
async def test_save_get_delete_roundtrip_never_returns_key(db):
    await _owner_row(db)
    from backend.api.app import create_app

    settings = make_settings(credential_key=_master())
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        saved = await client.put(
            "/api/v1/settings/ai-provider", headers=auth_headers(),
            json={"provider": "google_genai",
                  "model": "gemini-2.0-flash", "api_key": BOGUS_KEY})
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["configured"] is True
        assert body["provider"] == "google_genai"
        assert body["model"] == "gemini-2.0-flash"
        assert "api_key" not in body
        assert BOGUS_KEY not in saved.text
        assert "updated_at" in body

        fetched = await client.get(
            "/api/v1/settings/ai-provider", headers=auth_headers())
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["configured"] is True
        assert "api_key" not in fetched.json()

        deleted = await client.delete(
            "/api/v1/settings/ai-provider", headers=auth_headers())
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"removed": True}

        gone = await client.get(
            "/api/v1/settings/ai-provider", headers=auth_headers())
        assert gone.json()["configured"] is False


@pytest.mark.asyncio
async def test_save_rejects_unknown_provider_and_blank_key(db):
    from backend.api.app import create_app

    settings = make_settings(credential_key=_master())
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        bad_provider = await client.put(
            "/api/v1/settings/ai-provider", headers=auth_headers(),
            json={"provider": "nope", "model": "x", "api_key": "y"})
        assert bad_provider.status_code == 422, bad_provider.text
        assert bad_provider.json()["code"] == "INVALID_INPUT"

        blank_key = await client.put(
            "/api/v1/settings/ai-provider", headers=auth_headers(),
            json={"provider": "google_genai",
                  "model": "gemini-2.0-flash", "api_key": "   "})
        assert blank_key.status_code == 422, blank_key.text


@pytest.mark.asyncio
async def test_save_without_master_key_fails_closed(db):
    from backend.api.app import create_app

    settings = make_settings()
    assert settings.credential_key is None
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        response = await client.put(
            "/api/v1/settings/ai-provider", headers=auth_headers(),
            json={"provider": "google_genai",
                  "model": "gemini-2.0-flash", "api_key": BOGUS_KEY})
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "CREDENTIAL_STORE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_stored_key_is_ciphertext_in_database(db):
    await _owner_row(db)
    from backend.api.app import create_app
    from backend.persistence.database import session_scope
    from backend.persistence.repositories.pairing import (
        ProviderCredentialRepository,
    )

    from .conftest import OWNER

    settings = make_settings(credential_key=_master())
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    async with make_client(app) as client:
        saved = await client.put(
            "/api/v1/settings/ai-provider", headers=auth_headers(),
            json={"provider": "google_genai",
                  "model": "gemini-2.0-flash", "api_key": BOGUS_KEY})
        assert saved.status_code == 200, saved.text
    async with session_scope(db.sessions) as session:
        row = await ProviderCredentialRepository(session).get(OWNER)
    assert row is not None
    assert isinstance(row.key_encrypted, bytes)
    assert BOGUS_KEY.encode() not in row.key_encrypted
    assert row.key_encrypted != BOGUS_KEY.encode()


@pytest.mark.asyncio
async def test_live_call_with_bogus_key_fails_usefully(db):
    """Real network, fake credential: the failure mapping must hold."""
    from backend.api.app import create_app

    settings = make_settings(credential_key=_master())
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/settings/ai-provider/test", headers=auth_headers(),
            json={"provider": "google_genai",
                  "model": "gemini-2.0-flash", "api_key": BOGUS_KEY})
    assert response.status_code in (401, 502, 503), response.text
    body = response.json()
    assert body["code"] in (
        "PROVIDER_UNAUTHORIZED", "PROVIDER_BAD_RESPONSE",
        "PROVIDER_UNAVAILABLE",
    )
    assert BOGUS_KEY not in response.text


@pytest.mark.asyncio
async def test_candidate_success_path_with_stubbed_model(db, monkeypatch):
    """Wiring check: a working model yields ok + metadata, no secret."""
    import backend.provider_config.service as service_mod
    from backend.api.app import create_app

    class _Reply:
        content = "OK, all good here."

    class _StubModel:
        async def ainvoke(self, messages):
            assert messages
            return _Reply()

    monkeypatch.setattr(
        service_mod, "build_provider_model",
        lambda provider, model, api_key: _StubModel(),
    )
    settings = make_settings(credential_key=_master())
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        response = await client.post(
            "/api/v1/settings/ai-provider/test", headers=auth_headers(),
            json={"provider": "google_genai",
                  "model": "gemini-2.0-flash", "api_key": BOGUS_KEY})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["provider"] == "google_genai"
    assert body["model"] == "gemini-2.0-flash"
    assert "OK" in body["reply_preview"]
    assert BOGUS_KEY not in response.text


@pytest.mark.asyncio
async def test_test_saved_uses_stored_config(db, monkeypatch):
    await _owner_row(db)
    import backend.provider_config.service as service_mod
    from backend.api.app import create_app

    seen = {}

    class _Reply:
        content = "OK"

    class _StubModel:
        async def ainvoke(self, messages):
            return _Reply()

    def _fake_build(provider, model, api_key):
        seen.update(
            provider=provider, model=model, api_key=api_key)
        return _StubModel()

    monkeypatch.setattr(
        service_mod, "build_provider_model", _fake_build)
    settings = make_settings(credential_key=_master())
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        saved = await client.put(
            "/api/v1/settings/ai-provider", headers=auth_headers(),
            json={"provider": "google_genai",
                  "model": "gemini-2.0-flash", "api_key": BOGUS_KEY})
        assert saved.status_code == 200, saved.text
        response = await client.post(
            "/api/v1/settings/ai-provider/test", headers=auth_headers(),
            json={})
    assert response.status_code == 200, response.text
    assert seen == {"provider": "google_genai",
                    "model": "gemini-2.0-flash", "api_key": BOGUS_KEY}


def test_supported_providers_lists_gemini_first():
    assert list(SUPPORTED_PROVIDERS)[:1] == ["google_genai"]
