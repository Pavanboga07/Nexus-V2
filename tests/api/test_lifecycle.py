"""Application lifecycle: startup, shutdown, gateway wiring (real PG).

Lifespan owns every resource it starts and releases it in reverse.
Gateway live I/O runs against a real GatewayServer only when
explicitly enabled; startup failures dispose what was opened.
"""

import base64
import json

import pytest

from backend.api.app import (
    ApiState,
    build_production_state,
    close_state,
    create_app,
)
from backend.api.settings import ApiSettings
from backend.identity.identity import create_identity
from backend.identity.keys import public_key_to_bytes
from backend.persistence.database import session_scope
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.peers import PeerRepository
from gateway.app import GatewayServer

from .conftest import (  # noqa: F401
    OWNER,
    TOKEN,
    Keys,
    auth_headers,
    db,
    make_client,
    make_settings,
)


def _pub(identity):
    raw = public_key_to_bytes(identity.public_key)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.mark.asyncio
async def test_lifespan_builds_and_closes_state(db):
    settings = make_settings()
    app = create_app(settings)
    assert app.state.api is None
    async with app.router.lifespan_context(app):
        state = app.state.api
        assert state is not None
        assert state.sessions is not None
        assert state.engine is not None
        # Model, identities, and gateway I/O are unconfigured here.
        assert state.runtime is None
        assert state.conversations is None
        assert state.identities is None
        assert state.gateway_clients == {}
        assert state.dispatchers == []
    assert app.state.api is None


@pytest.mark.asyncio
async def test_startup_failure_disposes_engine(db, monkeypatch):
    from backend.api.app import build_production_state

    monkeypatch.setenv(
        "NEXUS_DATABASE_URL",
        "postgresql+asyncpg://nexus:nexus@127.0.0.1:1/nope")
    settings = make_settings()
    with pytest.raises(Exception):
        await build_production_state(settings)


@pytest.mark.asyncio
async def test_gateway_io_wiring_lifecycle(db, tmp_path):
    from .conftest import ensure_user

    await ensure_user(db, OWNER)
    keys = Keys()
    identity = keys.add(create_identity())
    async with session_scope(db.sessions) as session:
        await AgentRepository(session).create(
            identity.agent_id, _pub(identity), owner_user_id=OWNER)
    keys_path = tmp_path / "keys.json"
    keys_path.write_text(json.dumps({
        identity.agent_id: base64.b64encode(
            identity.private_key.private_bytes_raw()).decode("ascii"),
    }), encoding="utf-8")

    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        settings = make_settings(
            enable_gateway_io=True,
            gateway_url=f"ws://127.0.0.1:{server.port}",
            identity_keys_path=str(keys_path),
        )
        state = ApiState(settings=settings, sessions=db.sessions,
                         engine=db.engine)
        # Model is unconfigured, but gateway wiring must still come up.
        await state_scaffold(state, settings)
        try:
            assert set(state.gateway_clients) == {identity.agent_id}
            client = state.gateway_clients[identity.agent_id]
            from backend.gateway_client.models import ConnectionState

            assert client.state is ConnectionState.CONNECTED
            assert len(state.dispatchers) == 1
            dispatcher, task, receiver = state.dispatchers[0]
            assert dispatcher.running
            assert task is not None and not task.done()
            assert dispatcher._response_handler is not None
            assert state.worker is not None
            assert state.worker_task is not None
            assert not state.worker_task.done()
        finally:
            await close_state(state)
        assert state.gateway_clients == {}
        assert state.dispatchers == []
        assert state.worker is None
        assert state.worker_task is None
        assert client.state is not ConnectionState.CONNECTED
        assert not dispatcher.running
    finally:
        await server.stop()


async def state_scaffold(state, settings):
    """Production gateway wiring without the model pieces."""
    from backend.api.app import _open_gateway_io
    from backend.api.app import FileIdentityProvider

    if settings.identity_keys_path is not None:
        state.identities = FileIdentityProvider.from_file(
            settings.identity_keys_path)
    await _open_gateway_io(state, settings)


@pytest.mark.asyncio
async def test_gateway_io_requires_identities(db):
    settings = make_settings(enable_gateway_io=True,
                             gateway_url="ws://127.0.0.1:1")
    state = ApiState(settings=settings, sessions=db.sessions,
                     engine=db.engine)
    with pytest.raises(ValueError, match="IDENTITY_KEYS_PATH"):
        await state_scaffold(state, settings)
    await close_state(state)


@pytest.mark.asyncio
async def test_file_identity_provider_roundtrip(db, tmp_path):
    from backend.api.app import FileIdentityProvider

    keys = Keys()
    identity = keys.add(create_identity())
    keys_path = tmp_path / "keys.json"
    keys_path.write_text(json.dumps({
        identity.agent_id: base64.b64encode(
            identity.private_key.private_bytes_raw()).decode("ascii"),
    }), encoding="utf-8")
    provider = FileIdentityProvider.from_file(str(keys_path))
    assert provider.agent_ids == [identity.agent_id]
    assert (provider.private_key_for(identity.agent_id)
            .private_bytes_raw() == (
                identity.private_key.private_bytes_raw()))
    with pytest.raises(KeyError):
        provider.private_key_for("agent_nope")
    keys_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        FileIdentityProvider.from_file(str(keys_path))
    keys_path.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(ValueError):
        FileIdentityProvider.from_file(str(keys_path))
