"""ACK correlation for gateway rejections: real server, real client.

A rejection that carries the sender's own message id must resolve that
sender's exact pending waiter as a terminal FAILED ack — never hang it
until timeout, and never resolve a different waiter. Failures with no
recoverable id keep the bounded ack-timeout behavior (no false
success, no invented correlation).
"""

import asyncio
import json
from datetime import timedelta

import pytest

from backend.gateway_client.client import GatewayClient
from backend.gateway_client.errors import AckTimeoutError
from backend.gateway_client.models import ClientConfig
from backend.identity.identity import create_identity
from backend.identity.signing import sign_message
from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.serialization import message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from gateway.app import GatewayServer, utcnow
from gateway.models import GatewayConfig


def server_url(server):
    return f"ws://127.0.0.1:{server.port}"


def _envelope(identity, recipient_id, now, message_id, request_id="req_corr"):
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id,
        request_id=request_id,
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
async def test_rate_limited_ack_correlated_to_waiter():
    server = GatewayServer(
        "127.0.0.1", 0,
        GatewayConfig(rate_limit_max=1, rate_limit_window_s=60.0),
    )
    await server.start()
    try:
        alice, bob = create_identity(), create_identity()
        client = GatewayClient(alice, server_url(server))
        await client.connect()
        try:
            now = utcnow()
            first = await client.send_message(
                _envelope(alice, bob.agent_id, now, "mm_rl1"))
            assert first.status == "BUFFERED"
            ack = await client.send_message(
                _envelope(alice, bob.agent_id, now, "mm_rl2"))
            assert ack.message_id == "mm_rl2"
            assert ack.status == "FAILED"
            assert ack.detail.startswith("RATE_LIMITED")
            assert client.pending_acks == 0
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_malformed_envelope_ack_correlated_to_waiter():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice = create_identity()
        client = GatewayClient(alice, server_url(server))
        await client.connect()
        try:
            envelope = _envelope(
                alice, alice.agent_id, utcnow(), "mm_bad1", "req_bad1")
            del envelope["payload"]
            ack = await client.send_message(envelope)
            assert ack.message_id == "mm_bad1"
            assert ack.status == "FAILED"
            assert ack.detail.startswith("INVALID_MESSAGE")
            assert client.pending_acks == 0
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_uncorrelatable_rejection_times_out_without_success():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice = create_identity()
        client = GatewayClient(
            alice, server_url(server), ClientConfig(ack_timeout_s=5.0))
        await client.connect()
        try:
            # Valid string id, but garbage format: the gateway cannot
            # safely attribute it, so no waiter may resolve from it.
            with pytest.raises(AckTimeoutError):
                await client.send_message(
                    {"message_id": "!!!", "request_id": "req_x",
                     "bogus": True},
                    timeout=1.0,
                )
            assert client.pending_acks == 0
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unknown_frame_with_id_resolves_waiter_as_failed():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice, bob = create_identity(), create_identity()
        client = GatewayClient(alice, server_url(server))
        await client.connect()
        try:
            envelope = _envelope(
                alice, bob.agent_id, utcnow(), "mm_ping1", "req_ping1")
            waiter = asyncio.get_running_loop().create_future()
            client._pending["mm_ping1"] = waiter
            await client._ws.send(json.dumps({
                "type": "PING", "message": envelope,
            }))
            result = await asyncio.wait_for(waiter, 10)
            assert result.message_id == "mm_ping1"
            assert result.status == "FAILED"
            assert result.detail.startswith("UNKNOWN_FRAME")
            assert "mm_ping1" not in client._pending
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_bare_error_never_resolves_foreign_waiter():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        alice = create_identity()
        client = GatewayClient(alice, server_url(server))
        await client.connect()
        try:
            waiter = asyncio.get_running_loop().create_future()
            client._pending["mm_foreign"] = waiter
            await client._ws.send(json.dumps({"type": "PING"}))
            await asyncio.sleep(0.5)
            # The ERROR reply carries no id: the waiter must be intact.
            assert not waiter.done()
            assert "mm_foreign" in client._pending
            client._pending.pop("mm_foreign", None)
            if not waiter.done():
                waiter.cancel()
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_resolve_error_unit_semantics():
    client = GatewayClient(create_identity(), "ws://127.0.0.1:1")
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    client._pending["mm_u1"] = waiter
    client._resolve_error({
        "type": "ERROR", "code": "MALFORMED_FRAME",
        "message": "frame must be JSON", "message_id": "mm_u1",
        "request_id": "req_u1",
    })
    result = await asyncio.wait_for(waiter, 5)
    assert result.message_id == "mm_u1"
    assert result.status == "FAILED"
    assert result.detail.startswith("MALFORMED_FRAME")
    assert result.request_id == "req_u1"
    assert client.pending_acks == 0
    # Unknown ids and id-less frames resolve nothing and raise nothing.
    other = loop.create_future()
    client._pending["mm_kept"] = other
    client._resolve_error({"type": "ERROR", "code": "X",
                           "message_id": "mm_gone"})
    client._resolve_error({"type": "ERROR", "code": "X"})
    assert not other.done()
    client._pending.pop("mm_kept", None)
    other.cancel()
    await client.close()
