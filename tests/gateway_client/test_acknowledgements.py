"""ACK correlation and inbound demultiplexing against the real server."""

import asyncio
from datetime import timedelta

import pytest

from backend.gateway_client.client import GatewayClient
from backend.gateway_client.models import ClientConfig, ConnectionState
from backend.identity.identity import create_identity
from backend.identity.signing import sign_message
from backend.identity.verification import verify_message
from backend.protocol.models import NexusMessage, RemoteQueryPayload
from backend.protocol.serialization import message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType
from backend.protocol.validation import validate_message
from gateway.app import GatewayServer, utcnow


def server_url(server):
    return f"ws://127.0.0.1:{server.port}"


def _envelope(identity, recipient_id, now, message_id):
    message = NexusMessage(
        protocol_version=PROTOCOL_VERSION,
        message_id=message_id,
        request_id="req_corr",
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
async def test_send_preserves_signed_content():  # 4
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = create_identity()
        client = GatewayClient(identity, server_url(server))
        await client.connect()
        try:
            envelope = _envelope(identity, identity.agent_id, utcnow(), "mm_self")
            ack = await client.send_message(envelope)
            assert ack.message_id == "mm_self"
            assert ack.status == "DELIVERED"
            delivered = await asyncio.wait_for(client.incoming.get(), 10)
            assert message_to_dict(delivered) == envelope
            assert validate_message(delivered, now=utcnow()) == []
            assert verify_message(delivered, identity.public_key, now=utcnow()) is not None
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_acknowledgement_correlated_to_message_id():  # 5
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = create_identity()
        client = GatewayClient(identity, server_url(server))
        await client.connect()
        try:
            now = utcnow()
            ids = [f"mm_c{i}" for i in range(3)]
            acks = [
                await client.send_message(_envelope(identity, identity.agent_id, now, mid))
                for mid in ids
            ]
            assert [ack.message_id for ack in acks] == ids
            assert all(ack.status == "DELIVERED" for ack in acks)
            assert client.pending_acks == 0
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_concurrent_acks_not_miscorrelated():  # 6
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = create_identity()
        client = GatewayClient(identity, server_url(server))
        await client.connect()
        try:
            now = utcnow()
            envelopes = [
                _envelope(identity, identity.agent_id, now, f"mm_p{i}")
                for i in range(5)
            ]
            acks = await asyncio.gather(*[
                client.send_message(envelope) for envelope in envelopes
            ])
            got = {ack.message_id for ack in acks}
            assert got == {f"mm_p{i}" for i in range(5)}
            received = [
                await asyncio.wait_for(client.incoming.get(), 10)
                for _ in range(5)
            ]
            assert {m.message_id for m in received} == got
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_inbound_not_mistaken_for_acknowledgements():  # 7
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = create_identity()
        client = GatewayClient(identity, server_url(server))
        await client.connect()
        try:
            now = utcnow()
            first = _envelope(identity, identity.agent_id, now, "mm_a")
            second = _envelope(identity, identity.agent_id, now, "mm_b")
            ack_a = await client.send_message(first)
            ack_b = await client.send_message(second)
            assert (ack_a.message_id, ack_b.message_id) == ("mm_a", "mm_b")
            inbox = {
                (await asyncio.wait_for(client.incoming.get(), 10)).message_id,
                (await asyncio.wait_for(client.incoming.get(), 10)).message_id,
            }
            assert inbox == {"mm_a", "mm_b"}
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_inbound_queue_capacity_enforced():  # 8
    from backend.gateway_client.errors import ConnectionLostError

    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    try:
        identity = create_identity()
        client = GatewayClient(
            identity, server_url(server), ClientConfig(max_inbound_queue=1)
        )
        await client.connect()
        try:
            now = utcnow()
            first = await client.send_message(
                _envelope(identity, identity.agent_id, now, "mm_q1"))
            assert first.status == "DELIVERED"
            # The second DELIVER overflows the queue: the connection is
            # closed instead of silently dropping mail, and the pending
            # waiter fails safe rather than hanging or lying.
            with pytest.raises(ConnectionLostError):
                await client.send_message(
                    _envelope(identity, identity.agent_id, now, "mm_q2"))
            assert client.incoming.qsize() == 1
            kept = await asyncio.wait_for(client.incoming.get(), 10)
            assert kept.message_id == "mm_q1"
            await asyncio.sleep(0.3)
            assert client.state is not ConnectionState.CONNECTED
        finally:
            await client.close()
    finally:
        await server.stop()
