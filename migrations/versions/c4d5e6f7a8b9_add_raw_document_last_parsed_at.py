"""add raw_document.last_parsed_at

Revision ID: c4d5e6f7a8b9
Revises: 9be4126910d2
Create Date: 2026-08-27 10:30:00.000000

parse/verify 都要优先处理"从没处理过"的数据，再轮到处理过但结果不满意、
需要重试的数据。verify 已经有 resource.last_checked_at 可以当"处理过没有"
的信号，parse 这边缺一个对应字段，这里补上。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from funflix.models.base import UTCDateTime

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "9be4126910d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("raw_document", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_parsed_at", UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("raw_document", schema=None) as batch_op:
        batch_op.drop_column("last_parsed_at")
