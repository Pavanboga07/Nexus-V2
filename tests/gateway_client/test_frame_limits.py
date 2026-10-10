"""Sender-side frame-size enforcement: real server, exact boundaries.

The client measures the canonical UTF-8 bytes of the inner envelope —
the exact measure the gateway enforces — and rejects oversize payloads
before registering a waiter or moving any bytes. Boundary payloads are
built by exact byte arithmetic (ASCII padding adds precisely one byte
per character), then signed, so the sizes asserted are the sizes sent.
"""

import asyncio
from datetime import timedelta

import pytest

from backend.gateway_client.client import (
    GatewayClient,
    _canonical_envelope_size,
)
from backend.gateway_client.errors import MessageTooLargeError
from backend.gateway_client.models import ClientConfig, ConnectionState
from backend.identity.identity import create_identity
from backend.identity.signing import sign_message
from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.serialization import message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from gateway.app import GatewayServer, utcnow


def server_url(server):
    return f"ws://127.0.0.1:{server.port}"


def _sized_envelope(identity, recipient_id, now, message_id, total_size):
    """Signed envelope whose canonical size is exactly ``total_size``."""
    base = message_to_dict(sign_message(NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id,
        request_id="req_size",
        sender=identity.agent_id,
        recipient=recipient_id,
        type=MessageType.REMOTE_QUERY,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        payload=RemoteQueryPayload(query="q", requested_capabilities=[]),
        signature="",
    ), identity.private_key))
    padding = total_size - _canonical_envelope_size(base) + 1
    assert padding >= 0
    return message_to_dict(sign_message(NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id,
        request_id="req_size",
        sender=identity.agent_id,
        recipient=recipient_id,
        type=MessageType.REMOTE_QUERY,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        payload=RemoteQueryPayload(
            query="x" * padding, requested_capabilities=[]),
        signature="",
    ), identity.private_key))


@pytest.mark.asyncio
async def test_exact_limit_payload_dispatched():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice = create_identity()
        client = GatewayClient(alice, server_url(server))
        await client.connect()
        try:
            now = utcnow()
            envelope = _sized_envelope(
                alice, alice.agent_id, now, "mm_exact",
                client.config.max_send_size,
            )
            assert _canonical_envelope_size(envelope) == (
                client.config.max_send_size)
            ack = await client.send_message(envelope)
            assert ack.message_id == "mm_exact"
            assert ack.status == "DELIVERED"
            delivered = await asyncio.wait_for(client.incoming.get(), 10)
            assert delivered.message_id == "mm_exact"
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_over_limit_payload_rejected_before_transmission():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice = create_identity()
        client = GatewayClient(alice, server_url(server))
        await client.connect()
        try:
            now = utcnow()
            envelope = _sized_envelope(
                alice, alice.agent_id, now, "mm_big",
                client.config.max_send_size + 1,
            )
            assert _canonical_envelope_size(envelope) == (
                client.config.max_send_size + 1)
            transmitted = []
            original_send = client._ws.send

            async def recorder(data):
                transmitted.append(data)
                return await original_send(data)

            client._ws.send = recorder
            try:
                with pytest.raises(MessageTooLargeError):
                    await client.send_message(envelope)
            finally:
                client._ws.send = original_send
            # No waiter was registered and no byte moved.
            assert client.pending_acks == 0
            assert transmitted == []
            assert client.state is ConnectionState.CONNECTED
            # The connection is still healthy for normal traffic.
            small = _sized_envelope(
                alice, alice.agent_id, utcnow(), "mm_small", 512)
            ack = await client.send_message(small)
            assert ack.status == "DELIVERED"
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_configured_lower_limit_enforced():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice = create_identity()
        client = GatewayClient(
            alice, server_url(server),
            ClientConfig(max_send_size=1024),
        )
        await client.connect()
        try:
            envelope = _sized_envelope(
                alice, alice.agent_id, utcnow(), "mm_cfg", 1025)
            with pytest.raises(MessageTooLargeError):
                await client.send_message(envelope)
            assert client.pending_acks == 0
            ok = _sized_envelope(
                alice, alice.agent_id, utcnow(), "mm_cfg_ok", 1024)
            ack = await client.send_message(ok)
            assert ack.status == "DELIVERED"
        finally:
            await client.close()
    finally:
        await server.stop()
