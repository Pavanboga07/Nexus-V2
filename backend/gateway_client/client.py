"""Authenticated Gateway client for one local agent identity.

Protocol (exactly the Component 3 contract, no parallel invention):

- connect: open WebSocket → send HANDSHAKE {identity, signed
  CONNECT_REQUEST} → expect WELCOME for our own agent_id.
- send: MESSAGE {message} → await the ACK frame carrying the same
  ``message_id`` (correlated by one reader task; many concurrent
  senders are safe).
- receive: a single reader demultiplexes ACKs to their waiters and
  validated DELIVER envelopes into a bounded ``incoming`` queue for a
  future consumer. A full queue closes the connection rather than
  silently dropping application mail. Only this task ever calls
  ``recv()``; sends are serialized under a lock.

The client never modifies, re-signs, or interprets messages, and never
logs secrets, queries, or key material — only ids, frame types, and
safe codes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from backend.identity.models import AgentIdentity
from backend.identity.signing import sign_message
from backend.protocol.models import ConnectRequestPayload, NexusMessage
from backend.protocol.serialization import message_from_dict, message_to_dict
from backend.protocol.types import (
    PROTOCOL_VERSION,
    MessageType,
    generate_message_id,
    generate_request_id,
)

from .errors import (
    AckTimeoutError,
    ConnectionLostError,
    HandshakeFailedError,
    NotConnectedError,
)
from .models import AckResult, ClientConfig, ConnectionState

logger = logging.getLogger("nexus.gateway_client")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GatewayClient:
    """One authenticated, self-demultiplexing Gateway connection."""

    def __init__(
        self,
        identity: AgentIdentity,
        url: str,
        config: ClientConfig | None = None,
    ) -> None:
        self._identity = identity
        self._url = url
        self.config = config or ClientConfig()
        self._state = ConnectionState.DISCONNECTED
        self._ws: Any = None
        self._reader_task: Any = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self.incoming: asyncio.Queue = asyncio.Queue(
            maxsize=self.config.max_inbound_queue
        )
        # Claimed by the single inbound consumer (Component 9
        # dispatcher) while it owns this queue; None otherwise. The
        # slot survives reconnects: the consumer follows the current
        # queue. Transport behavior (reader, ACKs, queue rules) is
        # unchanged by this bookkeeping.
        self._inbound_owner: Any = None

    @property
    def agent_id(self) -> str:
        """The local identity this connection authenticates as."""
        return self._identity.agent_id

    @property
    def state(self) -> ConnectionState:
        """Current lifecycle state (read-only)."""
        return self._state

    @property
    def pending_acks(self) -> int:
        """Outstanding acknowledgement waiters (for tests/operators)."""
        return len(self._pending)

    def _handshake_frame(self) -> dict[str, Any]:
        """Build the Component 3 handshake using Component 2 signing."""
        now = _utcnow()
        message = NexusMessage(
            protocol_version=PROTOCOL_VERSION,
            message_id=generate_message_id(),
            request_id=generate_request_id(),
            sender=self._identity.agent_id,
            recipient=self.config.gateway_agent_id,
            type=MessageType.CONNECT_REQUEST,
            created_at=now,
            expires_at=now + timedelta(minutes=5),
            payload=ConnectRequestPayload(agent_id=self._identity.agent_id),
            signature="",
        )
        signed = sign_message(message, self._identity.private_key)
        return {
            "type": "HANDSHAKE",
            "identity": {
                "agent_id": self._identity.agent_id,
                "algorithm": "Ed25519",
                "public_key": self._public_key_b64(),
            },
            "message": message_to_dict(signed),
        }

    def _public_key_b64(self) -> str:
        raw = self._identity.public_key.public_bytes_raw()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    async def connect(self, timeout: float | None = None) -> None:
        """Open the socket and authenticate (idempotent when connected)."""
        if self._state is ConnectionState.CONNECTED:
            return
        self._state = ConnectionState.CONNECTING
        limit = timeout if timeout is not None else self.config.handshake_timeout_s
        try:
            ws = await ws_connect(
                self._url,
                open_timeout=limit,
                max_size=self.config.max_frame_size,
            )
        except Exception as exc:
            # Any connect failure uniformly means unreachable; the chained
            # original keeps diagnostics for operators.
            self._state = ConnectionState.DISCONNECTED
            raise ConnectionLostError(f"cannot reach gateway: {exc}") from exc
        self._ws = ws
        self.incoming = asyncio.Queue(maxsize=self.config.max_inbound_queue)
        self._pending = {}
        try:
            await ws.send(json.dumps(self._handshake_frame()))
            raw = await asyncio.wait_for(
                ws.recv(), timeout=self.config.handshake_timeout_s
            )
        except (asyncio.TimeoutError, ConnectionClosed) as exc:
            await self._close_socket()
            raise HandshakeFailedError("handshake timed out") from exc
        try:
            reply = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            await self._close_socket()
            raise HandshakeFailedError("handshake reply malformed") from exc
        if not isinstance(reply, dict) or reply.get("type") == "ERROR":
            await self._close_socket()
            raise HandshakeFailedError(
                f"handshake rejected: {reply.get('code', 'unknown')}"
                if isinstance(reply, dict)
                else "handshake rejected"
            )
        if (
            reply.get("type") != "WELCOME"
            or reply.get("agent_id") != self._identity.agent_id
        ):
            await self._close_socket()
            raise HandshakeFailedError("handshake reply unexpected")
        self._state = ConnectionState.CONNECTED
        self._reader_task = asyncio.get_running_loop().create_task(
            self._reader()
        )

    async def ensure_connected(self) -> None:
        """Connect unless already connected."""
        if self._state is not ConnectionState.CONNECTED:
            await self.connect()

    async def send_message(
        self, envelope: dict[str, Any], *, timeout: float | None = None
    ) -> AckResult:
        """Transmit one signed envelope; resolve its ACK by message_id.

        The envelope is serialized, never mutated. A timeout or a
        dropped connection means the outcome is AMBIGUOUS (bytes may be
        on the wire): callers must classify accordingly, never assume.
        """
        if self._state is not ConnectionState.CONNECTED or self._ws is None:
            raise NotConnectedError("client is not connected")
        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("message_id"), str
        ):
            raise ValueError("envelope must carry a string message_id")
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[envelope["message_id"]] = waiter
        try:
            async with self._send_lock:
                await self._ws.send(
                    json.dumps({"type": "MESSAGE", "message": envelope})
                )
        except (ConnectionClosed, OSError) as exc:
            self._pending.pop(envelope["message_id"], None)
            if not waiter.done():
                waiter.cancel()
            raise ConnectionLostError(
                f"send failed for {envelope['message_id']!r}"
            ) from exc
        limit = timeout if timeout is not None else self.config.ack_timeout_s
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=limit)
        except asyncio.TimeoutError as exc:
            self._pending.pop(envelope["message_id"], None)
            raise AckTimeoutError(
                f"no acknowledgement for {envelope['message_id']!r}"
            ) from exc
        except asyncio.CancelledError:
            # Caller went away: drop our waiter so it can never resolve
            # late into the wrong hands, then propagate cancellation.
            self._pending.pop(envelope["message_id"], None)
            if not waiter.done():
                waiter.cancel()
            raise

    async def close(self) -> None:
        """Close cleanly: fail pending waiters, reap the reader, no leaks."""
        task, self._reader_task = self._reader_task, None
        await self._close_socket()
        self._fail_pending(ConnectionLostError("connection closed"))
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._state = ConnectionState.CLOSED

    async def _close_socket(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._state is ConnectionState.CONNECTED:
            self._state = ConnectionState.DISCONNECTED

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for waiter in pending.values():
            if not waiter.done():
                waiter.set_exception(exc)

    async def _reader(self) -> None:
        """Single recv loop: ACKs to waiters, DELIVERs to the queue."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("dropping malformed inbound frame")
                    continue
                if not isinstance(frame, dict):
                    logger.warning("dropping non-object inbound frame")
                    continue
                kind = frame.get("type")
                if kind == "ACK":
                    self._resolve_ack(frame)
                elif kind == "DELIVER":
                    if not await self._accept_delivery(frame):
                        return
                elif kind == "ERROR":
                    logger.warning(
                        "gateway error %s", frame.get("code", "unknown")
                    )
                else:
                    logger.warning(
                        "unknown inbound frame type %r", kind
                    )
        except ConnectionClosed:
            pass
        finally:
            self._fail_pending(
                ConnectionLostError("connection lost while waiting")
            )
            if self._state is ConnectionState.CONNECTED:
                self._state = ConnectionState.DISCONNECTED

    def _resolve_ack(self, frame: dict[str, Any]) -> None:
        message_id = frame.get("message_id")
        waiter = self._pending.pop(message_id, None) if isinstance(
            message_id, str
        ) else None
        if waiter is None or waiter.done():
            logger.warning("unmatched acknowledgement %r", message_id)
            return
        waiter.set_result(
            AckResult(
                message_id=message_id,
                status=frame.get("status", ""),
                detail=frame.get("detail", ""),
                request_id=frame.get("request_id", ""),
            )
        )

    async def _accept_delivery(self, frame: dict[str, Any]) -> bool:
        """Validate one DELIVER and queue it; False means stop reading."""
        try:
            message = message_from_dict(frame.get("message"))
        except Exception as exc:
            # Deliberately broad: no malformed inbound mail may kill the
            # single reader task (availability over strictness here; the
            # message is dropped and logged, never processed).
            logger.warning("dropping invalid inbound message: %s", exc)
            return True
        logger.info(
            "inbound %s %s %s", message.message_id, message.request_id,
            message.type.value,
        )
        try:
            self.incoming.put_nowait(message)
        except asyncio.QueueFull:
            logger.error(
                "inbound queue full; closing connection rather than "
                "dropping %s", message.message_id,
            )
            try:
                await self._ws.close()
            except Exception:
                pass
            return False
        return True

    async def receive_inbound(
        self, timeout: float | None = None
    ) -> NexusMessage:
        """Next queued inbound message (future-consumer interface)."""
        if timeout is None:
            return await self.incoming.get()
        return await asyncio.wait_for(self.incoming.get(), timeout)
