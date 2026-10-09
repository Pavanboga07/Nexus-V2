"""Component 6 outbox hardening: claim leases, transmission flag, UNCERTAIN.

Revision ID: c60003
Revises: c50002

Existing rows receive transmission_started=false; claim columns start
NULL and are populated on the next claim. Downgrade folds any
UNCERTAIN rows into FAILED (preserved, never auto-retried) before
restoring the four-state CHECK, so live data cannot break the way down.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c60003"
down_revision = "c50002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox_messages",
        sa.Column("claim_token", sa.String(64), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "outbox_messages",
        sa.Column(
            "transmission_started",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.drop_constraint(
        "outbox_status_allowed", "outbox_messages", type_="check"
    )
    op.create_check_constraint(
        "outbox_status_allowed",
        "outbox_messages",
        "status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED', 'UNCERTAIN')",
    )


def downgrade() -> None:
    # UNCERTAIN has no meaning without the five-state contract: fold it
    # into FAILED (never auto-retried, preserved for reconciliation)
    # rather than failing the constraint restore on live data.
    op.execute(
        "UPDATE outbox_messages SET status = 'FAILED' "
        "WHERE status = 'UNCERTAIN'"
    )
    op.drop_constraint(
        "outbox_status_allowed", "outbox_messages", type_="check"
    )
    op.create_check_constraint(
        "outbox_status_allowed",
        "outbox_messages",
        "status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')",
    )
    op.drop_column("outbox_messages", "transmission_started")
    op.drop_column("outbox_messages", "lease_expires_at")
    op.drop_column("outbox_messages", "claim_token")
