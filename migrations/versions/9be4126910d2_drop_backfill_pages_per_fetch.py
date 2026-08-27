"""drop backfill_pages_per_fetch

Revision ID: 9be4126910d2
Revises: a1b2c3d4e5f6
Create Date: 2026-08-27 09:50:49.288475

补历史（backfill）改成每次 collect 都一口气扫到底，不再分轮限速，
这个字段就没用了。

autogenerate 顺带把 pg_trgm 那三个索引也标成"要删除"——它们是
a1b2c3d4e5f6 用 op.execute 建的原生 SQL 索引，不在 SQLAlchemy 的模型
元数据里，因此每次 autogenerate 都会误判成"模型里没有、该删掉"。
这里手工去掉了那三行，只保留真正要做的列删除。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9be4126910d2"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source", schema=None) as batch_op:
        batch_op.drop_column("backfill_pages_per_fetch")


def downgrade() -> None:
    with op.batch_alter_table("source", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "backfill_pages_per_fetch",
                sa.INTEGER(),
                server_default="20",
                autoincrement=False,
                nullable=False,
            )
        )
