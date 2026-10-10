"""Health endpoint tests: liveness open, readiness honest (real PG)."""

import pytest

from backend.api.app import create_app
from backend.api.settings import ApiSettings

from .conftest import db, make_client, make_settings, make_state  # noqa: F401


@pytest.mark.asyncio
async def test_live_needs_no_auth(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    async with make_client(app) as client:
        response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_reports_database(db):
    settings = make_settings()
    state, _ = make_state(db, settings)
    app = create_app(settings, state=state)
    async with make_client(app) as client:
        response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_ready_fails_closed_without_leak(db):
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.api.app import ApiState
    from backend.persistence.database import create_session_factory

    engine = create_async_engine(
        "postgresql+asyncpg://nexus:nexus@127.0.0.1:1/nope")
    try:
        settings = make_settings()
        state = ApiState(
            settings=settings,
            sessions=create_session_factory(engine),
            engine=engine,
        )
        app = create_app(settings, state=state)
        async with make_client(app) as client:
            response = await client.get("/api/v1/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body == {"status": "not-ready"}
        assert "nope" not in response.text
        assert "127.0.0.1" not in response.text
    finally:
        await engine.dispose()
