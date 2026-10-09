"""Remote execution engine: run approved inbound work, queue the answer.

One ``RemoteExecutor`` serves exactly one local agent id. :meth:`execute`
takes only a ``request_id`` and re-verifies everything against durable
state inside the claim transaction: inbound orientation, ``APPROVED``
status, a recorded recipient approval, live peer trust, and expiry.
The approved query then runs once through the injected real
:class:`AgentRuntime` (history always empty, so executions never share
conversations), and the signed ``REMOTE_RESPONSE`` plus outbox record
plus terminal state commit atomically.

Crash policy (no blind retries, no exactly-once claims):

1. before the durable claim — nothing stored; any retry is safe;
2. after the claim, before the agent runs — row is ``PROCESSING``;
   later calls report ``IN_PROGRESS`` and never re-enter the runtime;
3. while the model or MCP tools run — same: ``PROCESSING`` is sticky
   and ambiguous side effects are never auto-duplicated;
4. after the agent returns, before durable recording — same again;
5. after recording, before outbox delivery — the existing worker
   delivers; repeated calls replay the stored response id.

``PROCESSING`` never auto-advances here, and ``FAILED`` is terminal:
reprocessing requires operator action outside this component.
"""

from __future__ import annotations

import functools
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import OperationalError, TimeoutError as SQLTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.agent.errors import (
    InvalidInputError,
    InvalidModelOutputError,
    ModelInvocationError,
)
from backend.agent.runtime import AgentRuntime, AssistantResponse
from backend.identity.signing import sign_message
from backend.persistence.database import session_scope
from backend.persistence.errors import ConflictError
from backend.persistence.repositories.agents import AgentRepository
from backend.persistence.repositories.approvals import ApprovalRepository
from backend.persistence.repositories.messages import MessageRepository
from backend.persistence.repositories.outbox import OutboxRepository
from backend.persistence.repositories.peers import PeerRepository
from backend.persistence.repositories.remote_requests import (
    RemoteRequestRepository,
)
from backend.protocol.models import NexusMessage, RemoteResponsePayload
from backend.protocol.serialization import message_to_dict
from backend.protocol.types import PROTOCOL_VERSION, MessageType, is_valid_request_id
from backend.requests.errors import (
    InactiveAgentError,
    PeerError,
    RequestValidationError,
    UnknownAgentError,
    UnknownRequestError,
)
from backend.requests.models import RequestRecord
from backend.requests.ports import IdentityProvider
from backend.requests.states import (
    APPROVED,
    COMPLETED,
    EXPIRED,
    FAILED,
    PROCESSING,
)

from .errors import (
    ApprovalMissingError,
    ExecutionConflictError,
    ExecutionError,
    ExecutionInfrastructureError,
    NotInboundRequestError,
    RequestExpiredError,
    RequestNotApprovedError,
)
from .models import COMPLETED as OUT_COMPLETED
from .models import FAILED as OUT_FAILED
from .models import IN_PROGRESS as OUT_IN_PROGRESS
from .models import ExecutionOutcome

#: Static failure codes sent to the requester (never exception text).
CODE_EXECUTION_FAILED = "EXECUTION_FAILED"
CODE_REQUEST_EXPIRED = "REQUEST_EXPIRED"


def _translate_infrastructure_errors(fn):
    """Map connectivity-level failures to a retryable typed error."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (OperationalError, SQLTimeoutError, OSError) as exc:
            raise ExecutionInfrastructureError(
                f"transient infrastructure failure in {fn.__name__}"
            ) from exc

    return wrapper


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: Any, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RequestValidationError(f"{name} must be a timezone-aware datetime")
    return value


class RemoteExecutor:
    """Execute approved inbound requests exactly once per durable claim."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        local_agent_id: str,
        identities: IdentityProvider,
        runtime: AgentRuntime,
    ) -> None:
        """Wire one local agent to its signing identity and runtime.

        ``runtime`` is the recipient's real pre-built
        :class:`AgentRuntime` (model plus configured MCP allowlist
        tools); this component never builds models or opens MCP
        connections itself. ``history`` is always empty at invocation,
        so remote executions share no conversation with anything else.
        """
        self._sessions = sessions
        self._local_agent_id = local_agent_id
        self._identities = identities
        self._runtime = runtime

    @property
    def local_agent_id(self) -> str:
        """The agent id this executor runs approved work for."""
        return self._local_agent_id

    @_translate_infrastructure_errors
    async def execute(
        self, request_id: str, *, now: datetime | None = None
    ) -> ExecutionOutcome:
        """Claim, run, and durably answer one approved inbound request.

        Returns an :class:`ExecutionOutcome`; ``executed`` is True only
        when this call invoked the Agent Runtime. Rejected requests
        raise typed errors before any model or tool invocation.
        """
        current = _require_aware(now, "now") if now is not None else _utcnow()
        if not is_valid_request_id(request_id):
            raise RequestValidationError(f"invalid request_id: {request_id!r}")
        async with session_scope(self._sessions) as session:
            repo = RemoteRequestRepository(session)
            row = await repo.get_for_update(request_id)
            if row is None:
                raise UnknownRequestError(f"unknown request {request_id!r}")
            if row.status == COMPLETED:
                return self._replay(
                    row, OUT_COMPLETED, executed=False
                )
            if row.status == FAILED:
                return self._replay(row, OUT_FAILED, executed=False)
            if row.status == PROCESSING:
                return self._replay(row, OUT_IN_PROGRESS, executed=False)
            if row.status != APPROVED:
                raise RequestNotApprovedError(
                    f"request {request_id!r} in {row.status!r} "
                    "is not approved for execution"
                )
            agents = AgentRepository(session)
            recipient = await agents.get(row.recipient_agent_id)
            if (
                row.recipient_agent_id != self._local_agent_id
                or row.sender_agent_id == self._local_agent_id
                or recipient is None
                or not recipient.active
            ):
                raise NotInboundRequestError(
                    f"request {request_id!r} is not inbound work for "
                    "this executor"
                )
            sender = await agents.get(row.sender_agent_id)
            if sender is None:
                raise UnknownAgentError(
                    f"unknown sender {row.sender_agent_id!r}"
                )
            if not sender.active:
                raise InactiveAgentError(
                    f"sender {row.sender_agent_id!r} is deactivated"
                )
            approval = await ApprovalRepository(session).get_by_request_id(
                request_id
            )
            if (
                approval is None
                or approval.decision != "approved"
                or approval.approving_agent_id != self._local_agent_id
            ):
                raise ApprovalMissingError(
                    f"request {request_id!r} has no recipient approval"
                )
            peer = await PeerRepository(session).get(
                self._local_agent_id, row.sender_agent_id
            )
            if peer is None or peer.status != "connected":
                raise PeerError(
                    f"no connected peer relationship "
                    f"{row.sender_agent_id!r} -> {self._local_agent_id!r}"
                )
            if row.expires_at <= current:
                expired = True
            else:
                await repo.update(request_id, status=PROCESSING)
                expired = False
            query = row.query
            sender_id = row.sender_agent_id
            expires_at = row.expires_at
        if expired:
            # Expired before execution: record EXPIRED in its own
            # transaction (raising inside the claim scope above would
            # roll the transition back), unless the row moved on first.
            async with session_scope(self._sessions) as session:
                repo = RemoteRequestRepository(session)
                late = await repo.get_for_update(request_id)
                if late is not None and late.status == APPROVED:
                    await repo.update(request_id, status=EXPIRED)
            raise RequestExpiredError(
                f"request {request_id!r} expired before execution"
            )
        failure: tuple[str, str] | None = None
        answer: str | None = None
        try:
            result: AssistantResponse = await self._runtime.run(
                query, history=None
            )
            answer = result.text
        except InvalidInputError:
            failure = (
                CODE_EXECUTION_FAILED,
                "request exceeds runtime input limits",
            )
        except InvalidModelOutputError:
            failure = (
                CODE_EXECUTION_FAILED,
                "model returned no usable response",
            )
        except ModelInvocationError:
            failure = (
                CODE_EXECUTION_FAILED,
                "remote model invocation failed",
            )
        except Exception:  # noqa: BLE001 - safe static failure, chained
            failure = (
                CODE_EXECUTION_FAILED,
                "remote execution failed unexpectedly",
            )
        try:
            async with session_scope(self._sessions) as session:
                return await self._finalize(
                    session, request_id, sender_id, expires_at,
                    answer, failure,
                )
        except ConflictError as exc:
            return await self._classify_finalize_conflict(request_id)

    @staticmethod
    def _replay(
        row: Any, status: str, *, executed: bool
    ) -> ExecutionOutcome:
        """Report already-durable state without touching the runtime."""
        return ExecutionOutcome(
            status=status,
            request_id=row.request_id,
            response_message_id=row.response_message_id,
            executed=executed,
            record=RequestRecord.from_row(
                row, message_id=row.response_message_id
            ),
        )

    async def _finalize(
        self,
        session: AsyncSession,
        request_id: str,
        sender_id: str,
        expires_at: datetime,
        answer: str | None,
        failure: tuple[str, str] | None,
    ) -> ExecutionOutcome:
        """Persist response + outbox + terminal state atomically."""
        current = _utcnow()
        repo = RemoteRequestRepository(session)
        row = await repo.get_for_update(request_id)
        if row is None or row.status != PROCESSING:
            raise ExecutionConflictError(
                f"request {request_id!r} left processing during execution; "
                "no response recorded"
            )
        if failure is None and answer is not None and expires_at > current:
            payload = RemoteResponsePayload(status="ok", answer=answer)
            target_status: str = COMPLETED
            failure_code: str | None = None
            failure_message: str | None = None
        elif failure is not None:
            code, message = failure
            payload = RemoteResponsePayload(
                status="error",
                answer="",
                error_code=code,
                error_message=message,
            )
            target_status = FAILED
            failure_code, failure_message = code, message
        else:
            # The request expired mid-flight: no valid response envelope
            # can be built under the inherited-expiry rule, so record
            # the failure without queueing anything for the requester.
            updated = await repo.update(
                request_id,
                status=FAILED,
                completed_at=current,
                failure_code=CODE_REQUEST_EXPIRED,
                failure_message="request expired during execution",
            )
            assert updated is not None
            return ExecutionOutcome(
                status=OUT_FAILED,
                request_id=request_id,
                response_message_id=None,
                executed=True,
                record=RequestRecord.from_row(updated),
            )
        try:
            private_key = self._identities.private_key_for(
                self._local_agent_id
            )
        except KeyError as exc:
            raise ExecutionError(
                f"no signing identity for {self._local_agent_id!r}"
            ) from exc
        unsigned = NexusMessage(
            protocol_version=PROTOCOL_VERSION,
            message_id=f"msg_{uuid.uuid4().hex}",
            request_id=request_id,
            sender=self._local_agent_id,
            recipient=sender_id,
            type=MessageType.REMOTE_RESPONSE,
            created_at=current,
            expires_at=expires_at,
            payload=payload,
            signature="",
        )
        signed = sign_message(unsigned, private_key)
        wire = message_to_dict(signed)
        envelope = {
            key: value for key, value in wire.items() if key != "signature"
        }
        await MessageRepository(session).create(
            message_id=signed.message_id,
            request_id=request_id,
            sender_agent_id=signed.sender,
            recipient_agent_id=signed.recipient,
            message_type=signed.type.value,
            protocol_version=signed.protocol_version,
            created_at=signed.created_at,
            expires_at=signed.expires_at,
            signature=signed.signature,
            envelope=envelope,
        )
        await OutboxRepository(session).create(
            signed.message_id, available_at=current
        )
        updated = await repo.update(
            request_id,
            status=target_status,
            response_message_id=signed.message_id,
            completed_at=current,
            **(
                {"failure_code": failure_code,
                 "failure_message": failure_message}
                if failure_code is not None
                else {}
            ),
        )
        assert updated is not None
        return ExecutionOutcome(
            status=(
                OUT_COMPLETED if target_status == COMPLETED else OUT_FAILED
            ),
            request_id=request_id,
            response_message_id=signed.message_id,
            executed=True,
            record=RequestRecord.from_row(
                updated, message_id=signed.message_id
            ),
        )

    async def _classify_finalize_conflict(
        self, request_id: str
    ) -> ExecutionOutcome:
        """Classify a finalize-write conflict in a fresh transaction."""
        async with self._sessions() as session:
            row = await RemoteRequestRepository(session).get(request_id)
            if row is not None and row.status == COMPLETED:
                return self._replay(row, OUT_COMPLETED, executed=False)
            raise ExecutionConflictError(
                f"conflicting finalize for request {request_id!r}"
            ) from None
