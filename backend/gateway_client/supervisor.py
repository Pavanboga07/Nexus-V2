"""Reconnect supervisor for long-lived Gateway client sockets.

The base :class:`~backend.gateway_client.client.GatewayClient` opens on
demand and reports a lost connection, but it has no background loop.
That is enough for a one-shot send, not for an inbound socket: an idle
peer can lose its connection and never receive work again.

This supervisor owns exactly that policy and nothing else: poll the
client state, reconnect with jittered exponential backoff, and record
safe diagnostics. It never reads application mail, never executes
tools, and never closes the client on its own. The application remains
the owner of the client lifecycle.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

from .client import GatewayClient
from .models import ConnectionState


@dataclass(frozen=True)
class SupervisorConfig:
    """Backoff and polling bounds for one supervised client."""

    poll_interval_s: float = 1.0
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter_s: float = 0.5


class GatewaySupervisor:
    """Keep one GatewayClient connected while it is running."""

    def __init__(
        self,
        client: GatewayClient,
        config: SupervisorConfig | None = None,
    ) -> None:
        self._client = client
        self.config = config or SupervisorConfig()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._reconnects = 0
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def reconnects(self) -> int:
        """Successful reconnect attempts since :meth:`start`."""
        return self._reconnects

    @property
    def last_error(self) -> str | None:
        """Safe, non-secret description of the last reconnect failure."""
        return self._last_error

    async def start(self) -> None:
        """Start the background loop (idempotent)."""
        if self.running:
            return
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """Stop the loop; the client itself is left open for its owner."""
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def _next_delay(self, delay: float) -> float:
        jitter = self.config.jitter_s
        spread = random.uniform(0.0, jitter) if jitter > 0 else 0.0
        return min(self.config.max_delay_s, delay) + spread

    async def _run(self) -> None:
        delay = self.config.base_delay_s
        while not self._stopping:
            if self._client.state is ConnectionState.CLOSED:
                return
            if self._client.state is ConnectionState.CONNECTED:
                delay = self.config.base_delay_s
                await asyncio.sleep(self.config.poll_interval_s)
                continue
            try:
                await self._client.connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect is best effort
                self._last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(self._next_delay(delay))
                delay = min(self.config.max_delay_s, delay * 2)
                continue
            self._reconnects += 1
            self._last_error = None
            delay = self.config.base_delay_s
            await asyncio.sleep(self.config.poll_interval_s)
