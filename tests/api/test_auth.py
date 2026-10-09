"""Authentication tests: bearer gate, constant-time compare, fail-closed."""

import pytest

from backend.api.app import create_app
from backend.api.settings import ApiSettings

from .conftest import (  # noqa: F401
    TOKEN,
    auth_headers,
    db,
    make_client,
    make_settings,
    make_state,
)


@pytest.mark.asyncio
async def test_missing_token_rejected(db):
    app = create_app(make_settings(), state=make_state(db, make_settings())[0])
    async with make_client(app) as client:
        response = await client.get("/api/v1/agents")
    assert response.status_code == 401
    assert response.json() == {
        "code": "UNAUTHORIZED", "message": "authentication required"}


@pytest.mark.asyncio
async def test_wrong_token_rejected(db):
    settings = make_settings()
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        for bad in ("Bearer wrong", "Bearer " + TOKEN + "x",
                    "Token " + TOKEN, "Bearer"):
            response = await client.get(
                "/api/v1/agents", headers={"Authorization": bad})
            assert response.status_code == 401, bad
            assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_auth_not_configured_fails_closed(db):
    settings = ApiSettings()
    assert not settings.auth_configured
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        response = await client.get("/api/v1/agents",
                                    headers=auth_headers())
    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_health_stays_open_without_auth(db):
    settings = ApiSettings()
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        assert (await client.get("/api/v1/health/live")).status_code == 200


@pytest.mark.asyncio
async def test_token_never_echoed(db):
    settings = make_settings()
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        response = await client.get("/api/v1/agents",
                                    headers=auth_headers("x" * 64))
    assert response.status_code == 401
    assert TOKEN not in response.text
    assert "x" * 64 not in response.text
