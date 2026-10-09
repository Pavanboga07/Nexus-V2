"""Client connection lifecycle: reconnect, misuse, cancellation, cleanup."""

import asyncio
import ast
import pathlib

import pytest
from websockets.asyncio.server import serve as ws_serve

from backend.gateway_client.client import GatewayClient
from backend.gateway_client.errors import (
    ConnectionLostError,
    HandshakeFailedError,
    NotConnectedError,
)
from backend.gateway_client.models import ClientConfig, ConnectionState
from backend.identity.identity import create_identity
from gateway.app import GatewayServer


def server_url(server):
    return f"ws://127.0.0.1:{server.port}"


@pytest.mark.asyncio
async def test_reconnect_behavior():  # 10
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        client = GatewayClient(create_identity(), server_url(server))
        await client.connect()
        assert client.state is ConnectionState.CONNECTED
        await client.close()
        assert client.state is ConnectionState.CLOSED
        await client.connect()
        try:
            assert client.state is ConnectionState.CONNECTED
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_send_without_connection_rejected():
    from backend.gateway_client.errors import GatewayClientError

    client = GatewayClient(create_identity(), "ws://127.0.0.1:1")
    with pytest.raises(NotConnectedError):
        await client.send_message({"message_id": "m1"})
    assert isinstance(NotConnectedError("x"), GatewayClientError)
    await client.close()


@pytest.mark.asyncio
async def test_cancellation_and_shutdown_cleanup():  # 11
    async def black_hole(websocket):
        try:
            async for _ in websocket:
                pass
        except Exception:
            pass

    hole = await ws_serve(black_hole, "127.0.0.1", 0)
    port = hole.sockets[0].getsockname()[1]
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        client = GatewayClient(
            create_identity(), f"ws://127.0.0.1:{port}",
            ClientConfig(handshake_timeout_s=5.0),
        )
        # Handshake against the black hole times out: no reader, no leak.
        with pytest.raises(HandshakeFailedError):
            await client.connect()
        await client.close()
        assert client.pending_acks == 0
    finally:
        hole.close()
        await hole.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_disconnect_while_waiting_for_acknowledgement():  # 9
    received = []

    async def stub_server(websocket):
        async for raw in websocket:
            received.append(raw)
            # Never reply: the waiter must stay pending until we close.

    hole = await ws_serve(stub_server, "127.0.0.1", 0)
    port = hole.sockets[0].getsockname()[1]
    try:
        identity = create_identity()
        client = GatewayClient(
            identity, f"ws://127.0.0.1:{port}",
            ClientConfig(handshake_timeout_s=5.0),
        )
        # Bypass the handshake: open a raw socket and attach it.
        from websockets.asyncio.client import connect as ws_connect

        ws = await ws_connect(f"ws://127.0.0.1:{port}",
                              max_size=client.config.max_frame_size)
        client._ws = ws
        client._state = ConnectionState.CONNECTED
        client._reader_task = asyncio.get_running_loop().create_task(
            client._reader()
        )
        sender = asyncio.ensure_future(
            client.send_message({"message_id": "mm_wait"}, timeout=3600.0)
        )
        await asyncio.sleep(0.3)
        assert client.pending_acks == 1
        await client.close()
        with pytest.raises(ConnectionLostError):
            await sender
        assert client.pending_acks == 0
    finally:
        hole.close()
        await hole.wait_closed()


def _client_imports():
    modules = set()
    root = pathlib.Path(__file__).resolve().parent.parent.parent / "backend" / "gateway_client"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
    return modules


def test_client_has_clean_dependencies():
    modules = _client_imports()
    for banned in ("gateway", "langgraph", "langchain", "mcp", "frontend",
                   "fastapi", "sqlalchemy", "alembic", "redis"):
        assert banned not in modules, banned
    assert "backend" in modules  # protocol + identity only

    roots = set()
    root = pathlib.Path(__file__).resolve().parent.parent.parent / "backend" / "gateway_client"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.startswith("backend.")):
                roots.add(node.module.split(".")[1])
    assert roots <= {"protocol", "identity"}, roots
