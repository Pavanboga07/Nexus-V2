"""Pin conversation threads and sort history by recency.

Revision ID: c90001
Revises: c80005

The chat shell needs a user-controlled pinned flag. ``updated_at``
already existed and is now maintained when a turn is appended; listing
orders pinned threads first, then most recently active.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c90001"
down_revision = "c80005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("conversations", "pinned")
