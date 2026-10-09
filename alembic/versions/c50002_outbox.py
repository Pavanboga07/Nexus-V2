"""Component 5 persistence extension: outbox table + request conversation link.

Revision ID: c50002
Revises: c4init0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c50002"
down_revision = "c4init0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(1024), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')",
            name="outbox_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.message_id"],
            name="fk_outbox_messages_message_id_messages",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_messages"),
        sa.UniqueConstraint(
            "message_id", name="uq_outbox_messages_message_id"
        ),
    )
    op.create_index(
        "ix_outbox_status_available", "outbox_messages",
        ["status", "available_at"], unique=False,
    )
    op.add_column(
        "remote_requests",
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("remote_requests", "conversation_id")
    op.drop_table("outbox_messages")
