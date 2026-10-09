"""End-to-end Gateway integration: real server, two real Nexus clients.

Exercises the full Phase 7 path with fresh test identities and no
mocks: signed handshakes, valid delivery with byte-identical receipt,
a correlated failure ACK, a deterministic oversize rejection, and
queue preservation across reconnect — for both locally queued and
server-buffered mail.
"""

import asyncio
from datetime import timedelta

import pytest

from backend.gateway_client.client import GatewayClient
from backend.gateway_client.errors import MessageTooLargeError
from backend.gateway_client.models import ConnectionState
from backend.identity.identity import create_identity
from backend.identity.signing import sign_message
from backend.identity.verification import verify_message
from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.serialization import message_from_dict, message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from gateway.app import GatewayServer, utcnow
from gateway.models import GatewayConfig


def server_url(server):
    return f"ws://127.0.0.1:{server.port}"


def _envelope(sender, recipient_id, now, message_id, query="hello"):
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id,
        request_id="req_e2e",
        sender=sender.agent_id,
        recipient=recipient_id,
        type=MessageType.REMOTE_QUERY,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        payload=RemoteQueryPayload(query=query, requested_capabilities=[]),
        signature="",
    )
    return message_to_dict(sign_message(message, sender.private_key))


@pytest.mark.asyncio
async def test_full_integration_flow():
    server = GatewayServer(
        "127.0.0.1", 0,
        GatewayConfig(rate_limit_max=1000, rate_limit_window_s=60.0),
    )
    await server.start()
    try:
        alice, bob = create_identity(), create_identity()
        client_a = GatewayClient(alice, server_url(server))
        client_b = GatewayClient(bob, server_url(server))
        await client_a.connect()
        await client_b.connect()
        try:
            assert client_a.state is ConnectionState.CONNECTED
            assert client_b.state is ConnectionState.CONNECTED

            # 1. Valid signed message: receiver gets the original bytes.
            envelope = _envelope(
                alice, bob.agent_id, utcnow(), "mm_e2e1")
            ack = await client_a.send_message(envelope)
            assert ack.message_id == "mm_e2e1"
            assert ack.status == "DELIVERED"
            received = await asyncio.wait_for(
                client_b.incoming.get(), 10)
            assert message_to_dict(received) == envelope
            assert verify_message(
                received, alice.public_key, now=utcnow()) is not None

            # 2. Correlated failure ACK: exhausted rate budget on a
            # dedicated connection pair is timing-sensitive, so use a
            # tampered-but-identifiable envelope instead.
            forged = _envelope(alice, bob.agent_id, utcnow(), "mm_e2e2")
            forged["payload"] = dict(forged["payload"])
            forged["payload"]["query"] = "forged after signing"
            failure = await client_a.send_message(forged)
            assert failure.message_id == "mm_e2e2"
            assert failure.status == "FAILED"
            assert failure.detail.startswith("INVALID_SIGNATURE")
            assert client_a.pending_acks == 0

            # 3. Deterministic oversize rejection before transmission.
            big = _envelope(
                alice, bob.agent_id, utcnow(), "mm_e2e3",
                query="x" * (client_a.config.max_send_size + 1024),
            )
            with pytest.raises(MessageTooLargeError):
                await client_a.send_message(big)
            assert client_a.pending_acks == 0
            assert client_a.state is ConnectionState.CONNECTED

            # 4. Reconnect preserves locally queued mail: stage one
            # self-message, then close and reconnect before consuming.
            staged = await client_b.send_message(
                _envelope(bob, bob.agent_id, utcnow(), "mm_e2e4"))
            assert staged.status == "DELIVERED"
            await asyncio.sleep(0.3)
            queue_before = client_b.incoming
            await client_b.close()
            await client_b.connect()
            assert client_b.incoming is queue_before
            kept = await asyncio.wait_for(client_b.incoming.get(), 10)
            assert kept.message_id == "mm_e2e4"

            # 5. Server-buffered mail drains to the reconnected client:
            # send while bob is away, then reconnect and receive.
            await client_b.close()
            buffered = await client_a.send_message(
                _envelope(alice, bob.agent_id, utcnow(), "mm_e2e5"))
            assert buffered.status == "BUFFERED"
            await client_b.connect()
            drained = await asyncio.wait_for(client_b.incoming.get(), 10)
            assert drained.message_id == "mm_e2e5"
            assert message_to_dict(drained) == message_to_dict(
                message_from_dict(message_to_dict(drained)))
        finally:
            await client_a.close()
            await client_b.close()
    finally:
        await server.stop()
