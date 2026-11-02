"""Supervisor tests: an idle socket loss must reconnect without a send."""

import asyncio

import pytest

from backend.gateway_client.client import GatewayClient
from backend.gateway_client.models import ConnectionState
from backend.gateway_client.supervisor import (
    GatewaySupervisor,
    SupervisorConfig,
)
from backend.identity.identity import create_identity
from gateway.app import GatewayServer


@pytest.mark.asyncio
async def test_supervisor_reconnects_after_idle_socket_loss():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    identity = create_identity()
    client = GatewayClient(identity, f"ws://127.0.0.1:{server.port}")
    supervisor = GatewaySupervisor(
        client,
        SupervisorConfig(
            poll_interval_s=0.02,
            base_delay_s=0.02,
            max_delay_s=0.1,
            jitter_s=0.0,
        ),
    )
    try:
        await client.connect()
        assert client.state is ConnectionState.CONNECTED
        await supervisor.start()

        entry = server.registry.lookup(identity.agent_id)
        assert entry is not None
        await entry.transport.close()

        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            if (
                client.state is ConnectionState.CONNECTED
                and server.registry.lookup(identity.agent_id) is not None
            ):
                break
            await asyncio.sleep(0.02)
        assert client.state is ConnectionState.CONNECTED
        assert server.registry.lookup(identity.agent_id) is not None
        assert supervisor.reconnects >= 1
    finally:
        await supervisor.stop()
        await client.close()
        await server.stop()


@pytest.mark.asyncio
async def test_supervisor_stops_cleanly_and_does_not_resurrect_closed_client():
    server = GatewayServer("127.0.0.1", 0)
    await server.start()
    client = GatewayClient(create_identity(), f"ws://127.0.0.1:{server.port}")
    supervisor = GatewaySupervisor(
        client,
        SupervisorConfig(
            poll_interval_s=0.02,
            base_delay_s=0.02,
            max_delay_s=0.1,
            jitter_s=0.0,
        ),
    )
    try:
        await client.connect()
        await supervisor.start()
        await supervisor.stop()
        await client.close()
        await asyncio.sleep(0.1)
        assert client.state is ConnectionState.CLOSED
        assert not supervisor.running
    finally:
        await supervisor.stop()
        await client.close()
        await server.stop()
