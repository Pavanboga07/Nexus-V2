"""Component 4 initial schema: all nine Nexus tables.

Revision ID: c4init0001
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4init0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_table(
        "agents",
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("public_key", sa.String(64), nullable=False),
        sa.Column("key_algorithm", sa.String(16), nullable=False),
        sa.Column(
            "active", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"],
            name="fk_agents_owner_user_id_users",
        ),
        sa.PrimaryKeyConstraint("agent_id", name="pk_agents"),
    )
    op.create_index(
        "ix_agents_owner_user_id", "agents", ["owner_user_id"], unique=False
    )
    op.create_table(
        "agent_peers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("local_agent_id", sa.String(128), nullable=False),
        sa.Column("peer_agent_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "local_agent_id != peer_agent_id", name="no_self_peer"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'connected', 'revoked')",
            name="peer_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["local_agent_id"], ["agents.agent_id"],
            name="fk_agent_peers_local_agent_id_agents",
        ),
        sa.ForeignKeyConstraint(
            ["peer_agent_id"], ["agents.agent_id"],
            name="fk_agent_peers_peer_agent_id_agents",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_peers"),
        sa.UniqueConstraint(
            "local_agent_id", "peer_agent_id",
            name="uq_agent_peers_local_agent_id",
        ),
    )
    op.create_index(
        "ix_agent_peers_peer_agent_id", "agent_peers", ["peer_agent_id"],
        unique=False,
    )
    op.create_table(
        "remote_requests",
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("sender_agent_id", sa.String(128), nullable=False),
        sa.Column("recipient_agent_id", sa.String(128), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("requested_capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("response_message_id", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(
            ["sender_agent_id"], ["agents.agent_id"],
            name="fk_remote_requests_sender_agent_id_agents",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_agent_id"], ["agents.agent_id"],
            name="fk_remote_requests_recipient_agent_id_agents",
        ),
        sa.PrimaryKeyConstraint("request_id", name="pk_remote_requests"),
    )
    op.create_index(
        "ix_remote_requests_sender_agent_id", "remote_requests",
        ["sender_agent_id"], unique=False,
    )
    op.create_index(
        "ix_remote_requests_recipient_agent_id", "remote_requests",
        ["recipient_agent_id"], unique=False,
    )
    op.create_index(
        "ix_remote_requests_status", "remote_requests", ["status"],
        unique=False,
    )
    op.create_index(
        "ix_remote_requests_created_at", "remote_requests", ["created_at"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("message_id", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("sender_agent_id", sa.String(128), nullable=False),
        sa.Column("recipient_agent_id", sa.String(128), nullable=False),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("protocol_version", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature", sa.String(256), nullable=False),
        sa.Column("envelope", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"], ["remote_requests.request_id"],
            name="fk_messages_request_id_remote_requests",
        ),
        sa.PrimaryKeyConstraint("message_id", name="pk_messages"),
    )
    op.create_index(
        "ix_messages_request_id", "messages", ["request_id"], unique=False
    )
    op.create_index(
        "ix_messages_sender_agent_id", "messages", ["sender_agent_id"],
        unique=False,
    )
    op.create_index(
        "ix_messages_recipient_agent_id", "messages", ["recipient_agent_id"],
        unique=False,
    )
    op.create_index(
        "ix_messages_message_type", "messages", ["message_type"], unique=False
    )
    op.create_index(
        "ix_messages_created_at", "messages", ["created_at"], unique=False
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("approving_agent_id", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="approval_decision_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["remote_requests.request_id"],
            name="fk_approvals_request_id_remote_requests",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint("request_id", name="uq_approvals_request_id"),
    )
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.agent_id"],
            name="fk_conversations_agent_id_agents",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
    )
    op.create_index(
        "ix_conversations_agent_id", "conversations", ["agent_id"],
        unique=False,
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "role IN ('user', 'agent', 'system')",
            name="conversation_message_role_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"],
            name="fk_conversation_messages_conversation_id_conversations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_messages"),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id", "conversation_messages",
        ["conversation_id"], unique=False,
    )
    op.create_index(
        "ix_conversation_messages_request_id", "conversation_messages",
        ["request_id"], unique=False,
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_agent_id", sa.String(128), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("message_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_request_id", "audit_events", ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_message_id", "audit_events", ["message_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_created_at", "audit_events", ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    for table in (
        "audit_events",
        "conversation_messages",
        "conversations",
        "approvals",
        "messages",
        "remote_requests",
        "agent_peers",
        "agents",
        "users",
    ):
        op.drop_table(table)
