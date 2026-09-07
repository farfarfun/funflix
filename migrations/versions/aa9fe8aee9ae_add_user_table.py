"""add_user_table

Revision ID: aa9fe8aee9ae
Revises: 2b3c4d5e6f7a
Create Date: 2026-09-07 17:57:09.903366
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from funflix.models.base import UTCDateTime

revision: str = "aa9fe8aee9ae"
down_revision: str | None = "2b3c4d5e6f7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user")),
        sa.UniqueConstraint("username", name="uq_user_username"),
    )
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_created_at"))

    op.drop_table("user")
