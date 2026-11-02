"""Execution leases for approved remote work.

Revision ID: c80005
Revises: c70004

Adds the fencing token, expiry, heartbeat, and attempt counter used by
the execution worker. Existing PROCESSING rows keep NULL leases: they
predate the contract and are intentionally excluded from automatic
recovery. Downgrade drops the lease columns; no request status changes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c80005"
down_revision = "c70004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "remote_requests",
        sa.Column("execution_lease_owner", sa.String(128), nullable=True),
    )
    op.add_column(
        "remote_requests",
        sa.Column("execution_lease_token", sa.String(64), nullable=True),
    )
    op.add_column(
        "remote_requests",
        sa.Column(
            "execution_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "remote_requests",
        sa.Column(
            "execution_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "remote_requests",
        sa.Column(
            "execution_attempt",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_remote_requests_execution_lease_expires_at",
        "remote_requests",
        ["execution_lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_remote_requests_execution_lease_expires_at",
        table_name="remote_requests",
    )
    op.drop_column("remote_requests", "execution_attempt")
    op.drop_column("remote_requests", "execution_heartbeat_at")
    op.drop_column("remote_requests", "execution_lease_expires_at")
    op.drop_column("remote_requests", "execution_lease_token")
    op.drop_column("remote_requests", "execution_lease_owner")
