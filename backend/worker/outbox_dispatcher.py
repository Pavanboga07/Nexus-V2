"""Outbox dispatcher: claim → send → persist outcome. No execution, ever.

Batch cycle (``run_once``)::

    recover stale claims
      ↓
    claim PENDING rows (SKIP LOCKED, token + lease + attempt, COMMIT)
      ↓
    per claimed row, with the transaction always closed over the wire:
      load + validate + verify signature (Component 1/2, local keys)
      → mark transmission_started (conditional on token)
      → send over an authenticated pooled connection
      → await the correlated ACK
      ↓
    persist the graded outcome, conditional on still owning the claim:
      DELIVERED/BUFFERED ack → outbox SENT + request QUEUED→SENT (atomic)
      retryable pre-send failure → PENDING with bounded backoff
      permanent failure → outbox FAILED (+ request FAILED where fitting)
      ambiguous outcome → UNCERTAIN (preserved, never auto-retried)

Errors recorded or logged are static codes and ids only — never query
text, keys, URLs, credentials, or third-party exception strings.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from backend.gateway_client.client import GatewayClient
from backend.gateway_client.errors import (
    AckTimeoutError,
    ConnectionLostError,
    HandshakeFailedError,
    NotConnectedError,
)
from backend.gateway_client.models import ConnectionState
from backend.identity.errors import IdentityError
from backend.identity.verification import verify_message
from sqlalchemy.exc import OperationalError, TimeoutError as SQLTimeoutError
from backend.persistence.database import session_scope
from backend.persistence.errors import ConflictError, PersistenceError
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.protocol.serialization import (
    ProtocolSerializationError,
    message_from_dict,
    message_to_dict,
)
from backend.protocol.validation import ProtocolValidationError, validate_message
from backend.requests.errors import RetryableInfrastructureError
from backend.requests.ports import IdentityProvider
from backend.requests.service import RequestService
from backend.requests.states import is_terminal

from .config import WorkerConfig
from .errors import MissingIdentityError, WorkerError
from .models import DispatchOutcome, DispatchResult, compute_backoff

logger = logging.getLogger("nexus.worker")

_PERMANENT_ACK_CODES = frozenset({
    "INVALID_MESSAGE",
    "INVALID_SIGNATURE",
    "SENDER_MISMATCH",
    "UNKNOWN_RECIPIENT",
    "MESSAGE_TOO_LARGE",
})
_RETRYABLE_ACK_CODES = frozenset({
    "RATE_LIMITED",
    "BUFFER_FULL",
})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OutboxDispatcher:
    """Crash-safe async outbox-to-Gateway dispatcher."""

    def __init__(
        self,
        *,
        sessions: Any,
        identities: IdentityProvider,
        gateway_url: str,
        service: RequestService | None = None,
        config: WorkerConfig | None = None,
        client_factory: Any = None,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._sessions = sessions
        self._identities = identities
        self._gateway_url = gateway_url
        self._service = service
        self._config = config or WorkerConfig()
        self._client_factory = client_factory or self._default_client_factory
        self._clock = clock or _utcnow
        self._jitter = jitter or random.random
        self._pool: dict[str, Any] = {}
        self._pool_lock = asyncio.Lock()
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._stop_event = asyncio.Event()
        self._stopped = False

    # -- lifecycle ------------------------------------------------------

    async def run_once(self, batch_size: int | None = None) -> list[DispatchResult]:
        """Claim and dispatch one bounded batch; never raises for mail."""
        now = self._clock()
        try:
            await self.recover(now)
        except (OperationalError, SQLTimeoutError, OSError) as exc:
            raise RetryableInfrastructureError(
                "transient infrastructure failure in run_once"
            ) from exc
        limit = batch_size if batch_size is not None else self._config.batch_size
        try:
            async with session_scope(self._sessions) as session:
                claimed = await OutboxRepository(session).claim_pending(
                    limit=limit, now=now, lease_seconds=self._config.lease_seconds
                )
                snapshots = [
                    (row.message_id, row.claim_token, row.attempts)
                    for row in claimed
                ]
        except (OperationalError, SQLTimeoutError, OSError) as exc:
            raise RetryableInfrastructureError(
                "transient infrastructure failure in run_once"
            ) from exc
        results: list[DispatchResult] = []
        for message_id, token, attempts in snapshots:
            try:
                results.append(
                    await self._dispatch_one(message_id, token, attempts, now)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - last-resort guard
                # Traceback carries code lines only (never secret values);
                # the durable outcome below stays a static code.
                logger.exception(
                    "unexpected dispatch failure %s", message_id,
                )
                results.append(
                    await self._preserve_uncertain(
                        message_id, token, "UNEXPECTED_FAILURE", attempts
                    )
                )
        return results

    async def run_forever(self, poll_interval_s: float | None = None) -> None:
        """Poll batches until :meth:`stop`; cooperative, bounded, clean."""
        interval = (
            poll_interval_s
            if poll_interval_s is not None
            else self._config.poll_interval_s
        )
        try:
            while not self._stop_event.is_set():
                await self.run_once()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=interval
                    )
                    break
                except asyncio.TimeoutError:
                    continue
        finally:
            await self.close()

    async def stop(self) -> None:
        """Signal shutdown; the in-flight batch finishes, then links close.

        Cooperative by design: no claim is abandoned mid-send without
        its lease/recovery path, and no new batch starts afterwards.
        """
        self._stopped = True
        self._stop_event.set()

    async def close(self) -> None:
        """Close every pooled connection (idempotent)."""
        async with self._pool_lock:
            clients, self._pool = list(self._pool.values()), {}
        for client in clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - shutdown must not fail
                logger.warning("connection close failed")

    @property
    def stopped(self) -> bool:
        """True once :meth:`stop` was requested."""
        return self._stopped

    async def recover(
        self, now: datetime | None = None
    ) -> dict[str, list[str]]:
        """Recover expired-lease claims; report both destinations."""
        current = now if now is not None else self._clock()
        async with session_scope(self._sessions) as session:
            rows = await OutboxRepository(session).recover_stale_claims(
                current
            )
            pending = [r.message_id for r in rows if r.status == "PENDING"]
            uncertain = [r.message_id for r in rows if r.status == "UNCERTAIN"]
        if pending or uncertain:
            logger.warning(
                "recovered claims: %d pending, %d uncertain",
                len(pending), len(uncertain),
            )
        return {"pending": pending, "uncertain": uncertain}

    # -- dispatch ----------------------------------------------------------

    async def _dispatch_one(
        self, message_id: str, claim_token: str | None, attempts: int,
        now: datetime,
    ) -> DispatchResult:
        def result(
            request_id: str, outcome: DispatchOutcome, detail: str = "",
            at: datetime | None = None,
        ) -> DispatchResult:
            return DispatchResult(
                message_id=message_id, request_id=request_id,
                outcome=outcome, detail=detail, attempts=attempts,
                next_available_at=at,
            )

        # 1. Load + validate + verify (no writes yet).
        try:
            async with self._sessions() as session:
                message_row = await MessageRepository(session).get(message_id)
        except (OperationalError, SQLTimeoutError, OSError):
            # Read-only failure before any send: the claim is intact, so
            # a plain in-memory RETRYABLE is accurate (no DB write needed
            # or possible right now).
            return self._retryable_now(message_id, "?", attempts)
        if message_row is None:
            return await self._fail_permanent(
                message_id, claim_token, "?", attempts,
                "MESSAGE_ROW_MISSING",
                "persisted message row is missing",
            )
        request_id = message_row.request_id
        try:
            message = message_from_dict(
                {**message_row.envelope, "signature": message_row.signature}
            )
        except (ProtocolSerializationError, ProtocolValidationError):
            return await self._fail_permanent(
                message_id, claim_token, request_id, attempts,
                "MESSAGE_MALFORMED",
                "persisted envelope is not a valid protocol message",
            )
        if validate_message(message, now=now):
            return await self._fail_permanent(
                message_id, claim_token, request_id, attempts,
                "MESSAGE_INVALID",
                "persisted message fails protocol validation",
            )
        try:
            private_key = self._identities.private_key_for(message.sender)
        except KeyError:
            return await self._fail_permanent(
                message_id, claim_token, request_id, attempts,
                "IDENTITY_MISSING",
                "no local identity for the sender",
            )
        try:
            verify_message(message, private_key.public_key(), now=now)
        except (IdentityError, ProtocolValidationError,
                ProtocolSerializationError):
            return await self._fail_permanent(
                message_id, claim_token, request_id, attempts,
                "SIGNATURE_INVALID",
                "persisted message failed verification",
            )
        try:
            wire = message_to_dict(message)
        except ProtocolSerializationError:
            return await self._fail_permanent(
                message_id, claim_token, request_id, attempts,
                "MESSAGE_INVALID",
                "persisted message is not emittable",
            )

        # 2. Mark transmission_started BEFORE any bytes move (crash safety).
        try:
            started = await self._set_started(message_id, claim_token)
        except (OperationalError, SQLTimeoutError, OSError):
            # Flag write failed before any send: the claim is intact, so
            # an in-memory RETRYABLE is accurate (recovery re-drives it).
            return self._retryable_now(message_id, request_id, attempts)
        if not started:
            return result(request_id, DispatchOutcome.UNCERTAIN, "STALE_CLAIM")

        # 3. Authenticated transport (per-sender pooled connection).
        try:
            client = await self._client_for(message.sender)
            await client.ensure_connected()
        except (HandshakeFailedError, ConnectionLostError,
                NotConnectedError, OSError) as exc:
            return await self._reschedule(
                message_id, claim_token, request_id, attempts,
                f"CONNECT_FAILED:{type(exc).__name__}",
            )

        # 4. Single serialized transmit + correlated acknowledgement.
        try:
            ack = await client.send_message(
                wire, timeout=self._config.ack_timeout_s
            )
        except (AckTimeoutError, ConnectionLostError, OSError) as exc:
            return await self._mark_uncertain(
                message_id, claim_token, request_id, attempts,
                f"AMBIGUOUS_SEND:{type(exc).__name__}",
            )
        return await self._apply_ack(
            message_id, claim_token, request_id, attempts, ack, now,
        )

    async def _set_started(self, message_id: str, claim_token: str | None) -> bool:
        if claim_token is None:
            return False
        async with session_scope(self._sessions) as session:
            return await OutboxRepository(session).set_transmission_started(
                message_id, claim_token
            )

    def _retryable_now(
        self, message_id: str, request_id: str, attempts: int
    ) -> DispatchResult:
        """In-memory RETRYABLE when the database itself is unreachable.

        Used only before any send attempt, while the claim is provably
        intact: lease recovery will re-drive the row, so no durable
        write is needed (or possible) here.
        """
        now = self._clock()
        delay = compute_backoff(
            attempts, self._config.backoff_base_s,
            self._config.backoff_max_s, self._jitter(),
        )
        return DispatchResult(
            message_id=message_id, request_id=request_id,
            outcome=DispatchOutcome.RETRYABLE, detail="DB_UNAVAILABLE",
            attempts=attempts, next_available_at=now + timedelta(seconds=delay),
        )

    async def _reschedule(
        self, message_id: str, claim_token: str | None, request_id: str,
        attempts: int, code: str,
    ) -> DispatchResult:
        """Retryable pre-send failure: PENDING with bounded backoff."""
        now = self._clock()
        delay = compute_backoff(
            attempts, self._config.backoff_base_s,
            self._config.backoff_max_s, self._jitter(),
        )
        available = now + timedelta(seconds=delay)
        if claim_token is not None:
            async with session_scope(self._sessions) as session:
                await OutboxRepository(session).reschedule_if_claimed(
                    message_id, claim_token, available
                )
        logger.warning("retryable dispatch %s: %s", message_id, code)
        return DispatchResult(
            message_id=message_id, request_id=request_id,
            outcome=DispatchOutcome.RETRYABLE, detail=code,
            attempts=attempts, next_available_at=available,
        )

    async def _fail_permanent(
        self, message_id: str, claim_token: str | None, request_id: str,
        attempts: int, code: str, message: str,
    ) -> DispatchResult:
        """Terminal failure: outbox FAILED, then request FAILED if fated."""
        async with session_scope(self._sessions) as session:
            repo = OutboxRepository(session)
            if claim_token is not None:
                await repo.mark_failed_if_claimed(message_id, claim_token, code)
            else:
                row = await repo.get(message_id)
                if row is not None:
                    await repo.mark_failed(message_id, code)
        logger.error("permanent dispatch failure %s: %s", message_id, code)
        if request_id != "?" and self._service is not None:
            try:
                await self._service.fail(request_id, code, message)
            except Exception:  # noqa: BLE001 - recorded above; never lose the batch
                logger.warning("request failure recording failed %s", request_id)
        return DispatchResult(
            message_id=message_id, request_id=request_id,
            outcome=DispatchOutcome.PERMANENT, detail=code, attempts=attempts,
        )

    async def _mark_uncertain(
        self, message_id: str, claim_token: str | None, request_id: str,
        attempts: int, code: str,
    ) -> DispatchResult:
        """Ambiguous outcome: preserved, never auto-retried."""
        if claim_token is not None:
            async with session_scope(self._sessions) as session:
                await OutboxRepository(session).mark_uncertain_if_claimed(
                    message_id, claim_token, code
                )
        logger.error("uncertain dispatch %s: %s", message_id, code)
        return DispatchResult(
            message_id=message_id, request_id=request_id,
            outcome=DispatchOutcome.UNCERTAIN, detail=code, attempts=attempts,
        )

    async def _preserve_uncertain(
        self, message_id: str, claim_token: str | None, code: str,
        attempts: int,
    ) -> DispatchResult:
        """Last-resort guard for unexpected bugs: preserve, don't lose."""
        return await self._mark_uncertain(
            message_id, claim_token, "?", attempts, code
        )

    async def _apply_ack(
        self, message_id: str, claim_token: str | None, request_id: str,
        attempts: int, ack: Any, now: datetime,
    ) -> DispatchResult:
        status = ack.status
        if status in ("DELIVERED", "BUFFERED"):
            return await self._persist_confirmed(
                message_id, claim_token, request_id, attempts, now, status
            )
        code = _ack_code(ack.detail)
        if code == "EXPIRED":
            return await self._fail_expired(
                message_id, claim_token, request_id, attempts, now)
        if code in _PERMANENT_ACK_CODES:
            if code == "EXPIRED":
                return await self._fail_expired(
                    message_id, claim_token, request_id, attempts, now)
            return await self._fail_permanent(
                message_id, claim_token, request_id, attempts,
                f"GATEWAY_{code}", f"gateway rejected message: {code}",
            )
        if code in _RETRYABLE_ACK_CODES:
            return await self._reschedule(
                message_id, claim_token, request_id, attempts,
                f"GATEWAY_{code}",
            )
        if code == "REPLAYED":
            # The id was consumed before — possibly by our own pre-crash
            # attempt. Delivered or not is unprovable: preserve, don't retry.
            return await self._mark_uncertain(
                message_id, claim_token, request_id, attempts,
                "GATEWAY_REPLAYED",
            )
        return await self._mark_uncertain(
            message_id, claim_token, request_id, attempts,
            f"GATEWAY_UNKNOWN:{code}",
        )

    async def _persist_confirmed(
        self, message_id: str, claim_token: str | None, request_id: str,
        attempts: int, now: datetime, ack_status: str,
    ) -> DispatchResult:
        """Positive ACK: retry the DB persist, then grade honestly."""
        last_error: Exception | None = None
        for _ in range(3):
            try:
                terminal = await self._request_is_terminal(request_id)
                if self._service is not None and not terminal:
                    await self._service.mark_dispatched(
                        request_id, message_id, claim_token=claim_token or "",
                        sent_at=now, now=now,
                    )
                else:
                    async with session_scope(self._sessions) as session:
                        repo = OutboxRepository(session)
                        done = await repo.mark_sent_if_claimed(
                            message_id, claim_token or "", now)
                        if done is None:
                            raise WorkerError("claim no longer owned")
                logger.info("dispatched %s %s", message_id, ack_status)
                return DispatchResult(
                    message_id=message_id, request_id=request_id,
                    outcome=DispatchOutcome.SENT,
                    detail=f"GATEWAY_{ack_status}", attempts=attempts,
                )
            except Exception as exc:  # noqa: BLE001 - bounded persist retries
                last_error = exc
        logger.error(
            "handoff accepted but unrecorded %s: %s",
            message_id, type(last_error).__name__ if last_error else "?",
        )
        return await self._mark_uncertain(
            message_id, claim_token, request_id, attempts,
            "HANDOFF_UNRECORDED",
        )

    async def _request_is_terminal(self, request_id: str) -> bool:
        """True when the request already rests in a terminal state.

        Execution responses (Component 10) reference COMPLETED/FAILED
        requests: there is no onward request transition for them, so a
        confirmed handoff records the outbox row SENT and leaves the
        request untouched. Outbound QUEUED rows always take the
        mark_dispatched path exactly as before.
        """
        try:
            async with self._sessions() as session:
                row = await RemoteRequestRepository(session).get(request_id)
        except (OperationalError, SQLTimeoutError, OSError):
            return False
        return row is not None and is_terminal(row.status)

    async def _fail_expired(
        self, message_id: str, claim_token: str | None, request_id: str,
        attempts: int, now: datetime,
    ) -> DispatchResult:
        async with session_scope(self._sessions) as session:
            repo = OutboxRepository(session)
            if claim_token is not None:
                await repo.mark_failed_if_claimed(
                    message_id, claim_token, "GATEWAY_EXPIRED")
            else:
                await repo.mark_failed(message_id, "GATEWAY_EXPIRED")
        if self._service is not None:
            try:
                await self._service.expire(request_id, now=now)
            except Exception:  # noqa: BLE001 - outbox outcome already recorded
                logger.warning("request expiry recording failed %s", request_id)
        return DispatchResult(
            message_id=message_id, request_id=request_id,
            outcome=DispatchOutcome.PERMANENT, detail="GATEWAY_EXPIRED",
            attempts=attempts,
        )

    # -- connection pool -----------------------------------------------------

    def _default_client_factory(self, sender_id: str) -> GatewayClient:
        try:
            private_key = self._identities.private_key_for(sender_id)
        except KeyError as exc:
            raise MissingIdentityError(
                f"no local identity for {sender_id!r}"
            ) from exc
        from backend.identity.models import AgentIdentity
        from backend.gateway_client.models import ClientConfig

        return GatewayClient(
            AgentIdentity(agent_id=sender_id, private_key=private_key),
            self._gateway_url,
            ClientConfig(
                handshake_timeout_s=self._config.handshake_timeout_s),
        )

    async def _client_for(self, sender_id: str) -> Any:
        """Pooled per-sender connection; never duplicated under concurrency."""
        existing = self._pool.get(sender_id)
        if existing is not None and existing.state == ConnectionState.CONNECTED:
            if existing.agent_id != sender_id:
                raise WorkerError("pooled connection identity mismatch")
            return existing
        lock = self._agent_locks.get(sender_id)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[sender_id] = lock
        async with lock:
            existing = self._pool.get(sender_id)
            if existing is not None and existing.state == ConnectionState.CONNECTED:
                return existing
            if existing is not None:
                try:
                    await existing.close()
                except Exception:  # noqa: BLE001 - replace anyway
                    pass
                self._pool.pop(sender_id, None)
            client = self._client_factory(sender_id)
            if client.agent_id != sender_id:
                raise WorkerError(
                    f"client factory returned identity {client.agent_id!r} "
                    f"for sender {sender_id!r}"
                )
            self._pool[sender_id] = client
            return client


def _ack_code(detail: str) -> str:
    """Leading machine code from a Gateway FAILED detail ("CODE: ...")."""
    head, _, _ = (detail or "").partition(":")
    return head.strip().upper()
