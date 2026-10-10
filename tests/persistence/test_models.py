"""Model-declaration tests (no database; contract checks on metadata)."""

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import LargeBinary

from backend.persistence.models import (
    Agent,
    AgentPeer,
    Approval,
    AuditEvent,
    Base,
    Conversation,
    ConversationMessage,
    PairInvitation,
    PairLink,
    ProtocolMessage,
    ProviderCredential,
    RemoteRequest,
    User,
)


def _columns(model):
    return set(model.__table__.columns.keys())


def test_all_twelve_tables_declared():
    assert set(Base.metadata.tables) == {
        "users", "agents", "agent_peers", "remote_requests", "messages",
        "outbox_messages", "approvals", "conversations",
        "conversation_messages", "audit_events", "provider_credentials",
        "pair_invitations", "pair_links",
    }


def test_no_private_key_column_anywhere():
    for table in Base.metadata.tables.values():
        assert "private_key" not in table.columns
        assert "password" not in table.columns
        assert "secret" not in table.columns
        assert "api_key" not in table.columns


def test_provider_key_stored_encrypted_bytes_only():
    columns = ProviderCredential.__table__.columns
    assert isinstance(
        columns["key_encrypted"].type, LargeBinary)
    assert "key" not in {
        name for name in columns.keys() if name != "key_encrypted"
    }


def test_protocol_identifiers_are_primary_keys():
    assert list(Agent.__table__.primary_key.columns)[0].name == "agent_id"
    assert list(RemoteRequest.__table__.primary_key.columns)[0].name == (
        "request_id"
    )
    assert list(ProtocolMessage.__table__.primary_key.columns)[0].name == (
        "message_id"
    )


def test_message_envelope_and_signature_columns():
    assert "envelope" in _columns(ProtocolMessage)
    assert "signature" in _columns(ProtocolMessage)
    assert isinstance(
        ProtocolMessage.__table__.columns["envelope"].type, JSONB
    )


def test_foreign_keys_declared():
    inspector = {
        (fk.parent.table.name, fk.parent.name, fk.column.table.name)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
    }
    assert ("agents", "owner_user_id", "users") in inspector
    assert ("agent_peers", "local_agent_id", "agents") in inspector
    assert ("agent_peers", "peer_agent_id", "agents") in inspector
    assert ("remote_requests", "sender_agent_id", "agents") in inspector
    assert ("remote_requests", "recipient_agent_id", "agents") in inspector
    assert ("messages", "request_id", "remote_requests") in inspector
    assert ("approvals", "request_id", "remote_requests") in inspector
    assert ("conversations", "agent_id", "agents") in inspector
    assert (
        "conversation_messages",
        "conversation_id",
        "conversations",
    ) in inspector


def test_unique_constraints_declared():
    uniques = {
        (table.name, tuple(sorted(c.name for c in constraint.columns)))
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("agent_peers", ("local_agent_id", "peer_agent_id")) in uniques
    assert ("approvals", ("request_id",)) in uniques


def test_message_sender_recipient_are_not_foreign_keys():
    tables = Base.metadata.tables["messages"]
    constrained = {fk.parent.name for fk in tables.foreign_keys}
    assert "sender_agent_id" not in constrained
    assert "recipient_agent_id" not in constrained


def test_models_importable():
    for model in (User, Agent, AgentPeer, RemoteRequest, ProtocolMessage,
                  Approval, Conversation, ConversationMessage, AuditEvent,
                  ProviderCredential, PairInvitation, PairLink):
        assert model.__tablename__
