"""Hosted Gateway smoke test (manual URL, skipped by default).

Set ``NEXUS_TEST_GATEWAY_URL`` to the public Render hostname
(``wss://...onrender.com``) to verify a real browser-capable client can
open a WebSocket and complete the protocol handshake against the
hosted deployment. Without the variable the test skips; the local
end-to-end path is covered by ``test_local_demo.py``.
"""

from __future__ import annotations

import os

import pytest
from websockets.asyncio.client import connect as ws_connect

from backend.gateway_client.client import GatewayClient
from backend.identity.identity import create_identity

HOSTED_URL = os.environ.get("NEXUS_TEST_GATEWAY_URL", "").strip()

requires_hosted = pytest.mark.skipif(
    not HOSTED_URL,
    reason=(
        "set NEXUS_TEST_GATEWAY_URL=wss://...onrender.com "
        "to probe the hosted Gateway"
    ),
)


@pytest.mark.asyncio
@requires_hosted
async def test_hosted_gateway_handshake():
    assert HOSTED_URL.startswith("wss://"), HOSTED_URL
    identity = create_identity()
    client = GatewayClient(identity, HOSTED_URL)
    try:
        await client.connect(timeout=30.0)
    finally:
        await client.close()


@pytest.mark.asyncio
@requires_hosted
async def test_hosted_gateway_raw_websocket_opens():
    # No subprotocol, no handshake payload: proves the public route
    # reaches a live WebSocket listener at all.
    assert HOSTED_URL.startswith("wss://")
    host = HOSTED_URL.removeprefix("wss://")
    async with ws_connect(f"wss://{host}", open_timeout=30.0):
        pass
