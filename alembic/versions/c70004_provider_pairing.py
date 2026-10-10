"""Component 14 product tables: provider credentials and pairing state.

- ``provider_credentials``: one encrypted provider configuration per
  owner (the API key column holds Fernet ciphertext only).
- ``pair_invitations``: B-side issued invitations (secret SHA-256,
  expiry, single-use and revocation markers).
- ``pair_links``: each side's view of a pairing handshake.

Downgrade drops the three tables; all three hold only product-stage
rows (credentials must be re-entered after a downgrade — by design,
since plaintext recovery is not possible).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "c70004"
down_revision = "c60003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column(
            "owner_user_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("key_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "pair_invitations",
        sa.Column("invitation_id", sa.String(128), primary_key=True),
        sa.Column(
            "issuer_agent_id",
            sa.String(128),
            sa.ForeignKey("agents.agent_id"),
            nullable=False,
        ),
        sa.Column("secret_sha256", sa.String(64), nullable=False),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "used_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "revoked_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_pair_invitations_issuer_agent_id",
        "pair_invitations",
        ["issuer_agent_id"],
    )
    op.create_table(
        "pair_links",
        sa.Column(
            "id",
            PGUUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "local_agent_id",
            sa.String(128),
            sa.ForeignKey("agents.agent_id"),
            nullable=False,
        ),
        sa.Column("remote_agent_id", sa.String(128), nullable=False),
        sa.Column("remote_public_key", sa.String(256), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False,
            server_default="pending",
        ),
        sa.Column("invitation_id", sa.String(128), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'rejected', 'expired')",
            name="pair_link_status_allowed",
        ),
    )
    op.create_index(
        "ix_pair_links_local_agent_id",
        "pair_links",
        ["local_agent_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pair_links_local_agent_id", table_name="pair_links")
    op.drop_table("pair_links")
    op.drop_index(
        "ix_pair_invitations_issuer_agent_id",
        table_name="pair_invitations",
    )
    op.drop_table("pair_invitations")
    op.drop_table("provider_credentials")
