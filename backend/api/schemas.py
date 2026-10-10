"""Component 12 request/response schemas (strict, bounded, safe).

Every model forbids extra fields, bounds sizes against the same
limits the services enforce, and serializes only caller-safe data:
no signatures, keys, system instructions, or traces cross this
boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base: reject unknown fields instead of ignoring them."""

    model_config = ConfigDict(extra="forbid")


# -- shared ---------------------------------------------------------------

class ErrorBody(StrictModel):
    code: str
    message: str


class Page(StrictModel):
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=10000)


# -- health ----------------------------------------------------------------

class HealthBody(StrictModel):
    status: str


# -- agents -----------------------------------------------------------------

class AgentBody(StrictModel):
    agent_id: str
    active: bool
    owned: bool
    public_key: str
    key_algorithm: str


class PeerBody(StrictModel):
    peer_agent_id: str
    status: str


# -- provider configuration -------------------------------------------------

class ProviderSave(StrictModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=8192)


class ProviderStatus(StrictModel):
    configured: bool
    provider: str | None = None
    model: str | None = None
    updated_at: str | None = None


class ProviderTest(StrictModel):
    provider: str | None = Field(default=None, min_length=1, max_length=32)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    api_key: str | None = Field(default=None, min_length=1, max_length=8192)


class ProviderTestResult(StrictModel):
    ok: bool = True
    provider: str
    model: str
    latency_ms: int
    reply_preview: str


# -- pairing ------------------------------------------------------------------

class InvitationCreate(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=86400, ge=60, le=2592000)


class InvitationCreated(StrictModel):
    invitation_id: str
    invitation: str
    secret: str
    expires_at: str


class InvitationBody(StrictModel):
    invitation_id: str
    issuer_agent_id: str
    expires_at: str
    used: bool
    revoked: bool


class InvitationValidate(StrictModel):
    invitation: str = Field(min_length=1, max_length=8192)


class InvitationInfo(StrictModel):
    agent_id: str
    public_key: str
    fingerprint: str
    expires_at: str


class PairingAccept(StrictModel):
    invitation: str = Field(min_length=1, max_length=8192)
    sender_agent_id: str = Field(min_length=1, max_length=128)


class PairingBody(StrictModel):
    pairing_id: str
    local_agent_id: str
    remote_agent_id: str
    status: str
    invitation_id: str | None = None
    request_id: str | None = None


class PairingComplete(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)


class PairingCompletion(StrictModel):
    pairing_id: str
    approved: bool
    message_id: str
    ack: str


class PeerRevokeResult(StrictModel):
    revoked: int


# -- conversations ------------------------------------------------------------

class ConversationCreate(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=256)


class ConversationBody(StrictModel):
    id: str
    agent_id: str
    title: str | None
    created_at: datetime


class ConversationMessageBody(StrictModel):
    id: int
    role: str
    content: str
    created_at: datetime


class TurnCreate(StrictModel):
    message: str = Field(min_length=1, max_length=65536)


class TurnBody(StrictModel):
    conversation_id: str
    user_message_id: int
    assistant_message_id: int
    answer: str
    model: str


# -- remote requests ------------------------------------------------------------

class RequestCreate(StrictModel):
    sender_agent_id: str = Field(min_length=1, max_length=128)
    recipient_agent_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=65536)
    requested_capabilities: list[str] = Field(default_factory=list,
                                              max_length=64)
    # Caller-supplied id used verbatim (conflicts surface as 409;
    # explicitly NOT an idempotency key).
    request_id: str | None = Field(default=None, min_length=1,
                                   max_length=128)
    message_id: str | None = Field(default=None, min_length=1,
                                   max_length=128)
    # Lifetime of the request from now, in seconds.
    expires_in_seconds: int = Field(default=3600, ge=60,
                                    le=7 * 24 * 3600)
    # Optional sender-side chat thread to mirror into: the question is
    # posted when the request is created, the verified answer when the
    # signed response lands. Must belong to the sender agent.
    conversation_id: str | None = Field(default=None, min_length=1,
                                       max_length=128)


class RequestBody(StrictModel):
    request_id: str
    sender_agent_id: str
    recipient_agent_id: str
    query: str
    requested_capabilities: list[str]
    status: str
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    response_message_id: str | None = None
    # Present only for terminal requests with a stored answer.
    answer: str | None = None
    # Sender-side chat thread mirrored into, if the sender linked one.
    conversation_id: str | None = None


class ApprovalAction(StrictModel):
    reason: str | None = Field(default=None, min_length=1, max_length=1024)


class ExecutionBody(StrictModel):
    request_id: str
    status: str
    executed: bool
    response_message_id: str | None = None
    detail: str = ""


# -- serialization helpers -------------------------------------------------------

def agent_body(row: Any, owned: bool) -> AgentBody:
    return AgentBody(
        agent_id=row.agent_id,
        active=bool(row.active),
        owned=owned,
        public_key=row.public_key,
        key_algorithm=row.key_algorithm,
    )


def conversation_body(row: Any) -> ConversationBody:
    return ConversationBody(
        id=str(row.id),
        agent_id=row.agent_id,
        title=row.title,
        created_at=row.created_at,
    )


def message_body(row: Any) -> ConversationMessageBody:
    return ConversationMessageBody(
        id=row.id,
        role=row.role,
        content=row.content,
        created_at=row.created_at,
    )


def request_body(record: Any, answer: str | None = None) -> RequestBody:
    return RequestBody(
        request_id=record.request_id,
        sender_agent_id=record.sender_agent_id,
        recipient_agent_id=record.recipient_agent_id,
        query=record.query,
        requested_capabilities=list(record.requested_capabilities),
        status=record.status,
        created_at=record.created_at,
        expires_at=record.expires_at,
        completed_at=record.completed_at,
        failure_code=record.failure_code,
        failure_message=record.failure_message,
        response_message_id=record.response_message_id,
        answer=answer,
        conversation_id=(str(record.conversation_id)
                         if record.conversation_id is not None else None),
    )
