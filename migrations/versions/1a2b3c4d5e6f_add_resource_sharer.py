"""add resource sharer metadata

Revision ID: 1a2b3c4d5e6f
Revises: f6a7b8c9d0e1
Create Date: 2026-09-06 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("resource", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sharer_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("sharer_name", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("sharer_avatar_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("resource", schema=None) as batch_op:
        batch_op.drop_column("sharer_avatar_url")
        batch_op.drop_column("sharer_name")
        batch_op.drop_column("sharer_id")
