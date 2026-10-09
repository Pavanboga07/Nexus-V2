"""Client handshake tests against the real GatewayServer."""

import pytest

from backend.gateway_client.client import GatewayClient
from backend.gateway_client.errors import (
    ConnectionLostError,
    HandshakeFailedError,
)
from backend.gateway_client.models import ConnectionState
from backend.identity.identity import create_identity
from gateway.app import GatewayServer


def make_identity():
    return create_identity()


def server_url(server):
    return f"ws://127.0.0.1:{server.port}"


@pytest.mark.asyncio
async def test_successful_authenticated_connection():  # 1
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = make_identity()
        client = GatewayClient(identity, server_url(server))
        assert client.state is ConnectionState.DISCONNECTED
        await client.connect()
        try:
            assert client.state is ConnectionState.CONNECTED
            assert client.agent_id == identity.agent_id
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_invalid_identity_rejected():  # 2
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = make_identity()
        client = GatewayClient(identity, server_url(server))
        client._handshake_frame = lambda: {"type": "HANDSHAKE"}
        with pytest.raises(HandshakeFailedError):
            await client.connect()
        assert client.state is not ConnectionState.CONNECTED
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_failed_handshake_handled():  # 3
    identity = make_identity()
    client = GatewayClient(identity, "ws://127.0.0.1:1")
    with pytest.raises(ConnectionLostError):
        await client.connect(timeout=2.0)
    assert client.state is ConnectionState.DISCONNECTED
    # A failed client is still usable afterwards.
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        live = GatewayClient(identity, server_url(server))
        await live.connect()
        try:
            assert live.state is ConnectionState.CONNECTED
        finally:
            await live.close()
    finally:
        await server.stop()
        await client.close()
