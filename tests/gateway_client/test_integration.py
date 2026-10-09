"""Client transport integration: real frames, real gateway, stub peer."""

import asyncio
import json
from datetime import timedelta

import pytest
from websockets.asyncio.client import connect as ws_connect

from backend.gateway_client.client import GatewayClient
from backend.identity.identity import create_identity
from backend.identity.signing import sign_message
from backend.identity.verification import verify_message
from backend.protocol.models import (
    ConnectRequestPayload,
    NexusMessage,
    RemoteQueryPayload,
)
from backend.protocol.serialization import message_from_dict, message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from gateway.app import GatewayServer, utcnow


def server_url(server):
    return f"ws://127.0.0.1:{server.port}"


async def _stub_peer(url, identity, inbox):
    """Minimal agent connection: handshake, then collect DELIVER frames."""
    ws = await ws_connect(url)
    now = utcnow()
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id="hs_stub",
        request_id="hs_stub_req",
        sender=identity.agent_id,
        recipient="gateway",
        type=MessageType.CONNECT_REQUEST,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        payload=ConnectRequestPayload(agent_id=identity.agent_id),
        signature="",
    )
    await ws.send(json.dumps({
        "type": "HANDSHAKE",
        "identity": identity.public_identity().to_dict(),
        "message": message_to_dict(sign_message(message, identity.private_key)),
    }))
    first = json.loads(await asyncio.wait_for(ws.recv(), 10))
    assert first["type"] == "WELCOME"

    async def drain():
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") == "DELIVER":
                    inbox.append(frame["message"])
        except Exception:
            pass

    task = asyncio.get_running_loop().create_task(drain())
    return ws, task


@pytest.mark.asyncio
async def test_end_to_end_send_deliver_verify():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice, bob = create_identity(), create_identity()
        inbox: list = []
        ws_bob, drain_task = await _stub_peer(server_url(server), bob, inbox)
        try:
            client = GatewayClient(alice, server_url(server))
            await client.connect()
            try:
                now = utcnow()
                message = NexusMessage(
                    protocol_version=PROTOCOL_VERSION,
                    message_id="mm_e2e",
                    request_id="req_e2e",
                    sender=alice.agent_id,
                    recipient=bob.agent_id,
                    type=MessageType.REMOTE_QUERY,
                    created_at=now,
                    expires_at=now + timedelta(minutes=30),
                    payload=RemoteQueryPayload(
                        query="projects?", requested_capabilities=[]),
                    signature="",
                )
                envelope = message_to_dict(
                    sign_message(message, alice.private_key))
                ack = await client.send_message(envelope)
                assert ack.status == "DELIVERED"
                await asyncio.sleep(0.5)
                assert len(inbox) == 1
                received = inbox[0]
                assert received["message_id"] == "mm_e2e"
                assert received["request_id"] == "req_e2e"
                assert received["signature"] == envelope["signature"]
                assert received == envelope
                rebuilt = message_from_dict(received)
                assert verify_message(
                    rebuilt, alice.public_key, now=utcnow()) is not None
            finally:
                await client.close()
        finally:
            await ws_bob.close()
            drain_task.cancel()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_offline_recipient_buffered_outcome():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice, bob = create_identity(), create_identity()
        client = GatewayClient(alice, server_url(server))
        await client.connect()
        try:
            now = utcnow()
            message = NexusMessage(
                protocol_version=PROTOCOL_VERSION,
                message_id="mm_off",
                request_id="req_off",
                sender=alice.agent_id,
                recipient=bob.agent_id,
                type=MessageType.REMOTE_QUERY,
                created_at=now,
                expires_at=now + timedelta(minutes=30),
                payload=RemoteQueryPayload(query="hi", requested_capabilities=[]),
                signature="",
            )
            envelope = message_to_dict(
                sign_message(message, alice.private_key))
            ack = await client.send_message(envelope)
            assert ack.status == "BUFFERED"
        finally:
            await client.close()
    finally:
        await server.stop()
