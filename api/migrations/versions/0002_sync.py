"""sync: key envelopes, per-account sequence state, end-to-end encrypted records

Revision ID: 0002_sync
Revises: 0001_accounts
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_sync"
down_revision = "0001_accounts"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "sync_keys",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("envelope", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "sync_state",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("bytes_used", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "sync_records",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("collection", sa.String(64), primary_key=True),
        sa.Column("record_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("user_id", "seq", name="uq_sync_records_user_seq"),
    )


def downgrade() -> None:
    for table in ("sync_records", "sync_state", "sync_keys"):
        op.drop_table(table)
