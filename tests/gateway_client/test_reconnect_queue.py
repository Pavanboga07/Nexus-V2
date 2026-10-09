"""Incoming queue preservation across reconnects: real server.

The inbound queue is created once per client and never replaced by
``connect()``. Messages already received and queued must survive a
close/reconnect cycle exactly once; reconnecting with an empty queue
must stay functional; concurrent reconnects must converge on one live
connection without losing or duplicating queued mail.
"""

import asyncio
from datetime import timedelta

import pytest
from websockets.asyncio.server import serve as ws_serve

from backend.gateway_client.client import GatewayClient
from backend.gateway_client.errors import HandshakeFailedError
from backend.gateway_client.models import ClientConfig, ConnectionState
from backend.identity.identity import create_identity
from backend.identity.signing import sign_message
from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.serialization import message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from gateway.app import GatewayServer, utcnow


def server_url(server):
    return f"ws://127.0.0.1:{server.port}"


def _envelope(identity, recipient_id, now, message_id):
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id,
        request_id="req_q",
        sender=identity.agent_id,
        recipient=recipient_id,
        type=MessageType.REMOTE_QUERY,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        payload=RemoteQueryPayload(query="ping", requested_capabilities=[]),
        signature="",
    )
    return message_to_dict(sign_message(message, identity.private_key))


@pytest.mark.asyncio
async def test_queued_message_survives_close_and_reconnect():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = create_identity()
        client = GatewayClient(identity, server_url(server))
        await client.connect()
        queue_before = client.incoming
        try:
            ack = await client.send_message(
                _envelope(identity, identity.agent_id, utcnow(), "mm_keep"))
            assert ack.status == "DELIVERED"
            kept = await asyncio.wait_for(client.incoming.get(), 10)
            assert kept.message_id == "mm_keep"
            # Queue a second message, then reconnect before consuming it.
            ack2 = await client.send_message(
                _envelope(identity, identity.agent_id, utcnow(), "mm_kept2"))
            assert ack2.status == "DELIVERED"
            await asyncio.sleep(0.3)
            assert client.incoming.qsize() == 1
            await client.close()
            await client.connect()
            # Same queue object, message intact, delivered exactly once.
            assert client.incoming is queue_before
            assert client.state is ConnectionState.CONNECTED
            redelivered = await asyncio.wait_for(client.incoming.get(), 10)
            assert redelivered.message_id == "mm_kept2"
            assert client.incoming.qsize() == 0
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_reconnect_with_empty_queue_stays_functional():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = create_identity()
        client = GatewayClient(identity, server_url(server))
        await client.connect()
        queue_before = client.incoming
        await client.close()
        await client.connect()
        assert client.incoming is queue_before
        assert client.incoming.qsize() == 0
        ack = await client.send_message(
            _envelope(identity, identity.agent_id, utcnow(), "mm_after"))
        assert ack.status == "DELIVERED"
        assert (await asyncio.wait_for(client.incoming.get(), 10)
                ).message_id == "mm_after"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_concurrent_reconnects_converge_without_loss():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = create_identity()
        client = GatewayClient(identity, server_url(server))
        await client.connect()
        queue_before = client.incoming
        ack = await client.send_message(
            _envelope(identity, identity.agent_id, utcnow(), "mm_race"))
        assert ack.status == "DELIVERED"
        await asyncio.sleep(0.3)
        await client.close()
        await asyncio.gather(*[client.connect() for _ in range(5)])
        assert client.state is ConnectionState.CONNECTED
        assert client.incoming is queue_before
        kept = await asyncio.wait_for(client.incoming.get(), 10)
        assert kept.message_id == "mm_race"
        assert client.incoming.qsize() == 0
        # Exactly one live connection still serves traffic.
        ack2 = await client.send_message(
            _envelope(identity, identity.agent_id, utcnow(), "mm_race2"))
        assert ack2.status == "DELIVERED"
        assert (await asyncio.wait_for(client.incoming.get(), 10)
                ).message_id == "mm_race2"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_failed_handshake_leaves_retryable_state():
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
        with pytest.raises(HandshakeFailedError):
            await client.connect()
        # Not stuck mid-handshake: a later attempt may proceed normally.
        assert client.state is ConnectionState.DISCONNECTED
        await client.close()
        real = GatewayClient(create_identity(), server_url(server))
        await real.connect()
        try:
            assert real.state is ConnectionState.CONNECTED
        finally:
            await real.close()
    finally:
        hole.close()
        await hole.wait_closed()
        await server.stop()
