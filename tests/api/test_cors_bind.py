"""Deployment safety: CORS, bind defaults, OpenAPI contract (no PG)."""

import uuid

import pytest

from backend.api.app import create_app
from backend.api.settings import ApiSettings


def test_no_permissive_cors_by_default():
    app = create_app(ApiSettings(api_token="t",
                                 owner_user_id=uuid.uuid4()))
    middlewares = [getattr(m, "cls", m) for m in app.user_middleware]
    names = [getattr(cls, "__name__", str(cls)) for cls in middlewares]
    assert "CORSMiddleware" not in names
    from starlette.middleware.cors import CORSMiddleware

    assert not any(
        isinstance(m, CORSMiddleware) for m in app.user_middleware)


def test_host_defaults_to_loopback():
    assert ApiSettings().host == "127.0.0.1"
    assert ApiSettings.from_env({}).host == "127.0.0.1"


def test_settings_reject_malformed_values():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        ApiSettings.from_env({"NEXUS_API_PORT": "nine"})
    with _pytest.raises(ValueError):
        ApiSettings.from_env({"NEXUS_API_PORT": "0"})
    with _pytest.raises(ValueError):
        ApiSettings.from_env({"NEXUS_API_OWNER_USER_ID": "nope"})
    settings = ApiSettings.from_env({
        "NEXUS_API_TOKEN": "  secret  ",
        "NEXUS_API_OWNER_USER_ID": "12345678-1234-5678-1234-567812345678",
        "NEXUS_API_ENABLE_GATEWAY_IO": "yes",
    })
    assert settings.api_token == "secret"
    assert settings.auth_configured is True
    assert settings.enable_gateway_io is True
    assert ApiSettings.from_env({}).auth_configured is False


def test_openapi_matches_implemented_routes():
    app = create_app(ApiSettings(api_token="t",
                                 owner_user_id=uuid.uuid4()))
    paths = set(app.openapi()["paths"])
    expected = {
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/agents",
        "/api/v1/agents/{agent_id}/peers",
        "/api/v1/conversations",
        "/api/v1/conversations/{conversation_id}/messages",
        "/api/v1/conversations/{conversation_id}/turns",
        "/api/v1/requests",
        "/api/v1/requests/{request_id}",
        "/api/v1/approvals/pending",
        "/api/v1/requests/{request_id}/approve",
        "/api/v1/requests/{request_id}/reject",
        "/api/v1/requests/{request_id}/execute",
    }
    assert expected <= paths
    # No WebSocket event stream or frontend route exists.
    assert not any("websocket" in path.lower() for path in paths)
    assert not any(path.endswith((".html", ".js", ".css"))
                   for path in paths)


@pytest.mark.asyncio
async def test_unknown_route_shape(db):
    from .conftest import make_client, make_settings, make_state

    settings = make_settings()
    app = create_app(settings, state=make_state(db, settings)[0])
    async with make_client(app) as client:
        response = await client.get("/api/v1/nope",
                                    headers={"Authorization": "Bearer x"})
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
