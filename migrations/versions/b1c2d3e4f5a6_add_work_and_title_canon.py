"""归一迁移 A：新增 work / title_canon 表，media 加 work_id + season

Revision ID: b1c2d3e4f5a6
Revises: d3e4f5a6b7c8
Create Date: 2026-10-05

这是「一部剧一条、季作为子层」改造的第一步，**纯加法、完全可逆**：

- 新增 `work`（作品/系列）与 `title_canon`（LLM 归一裁决缓存）两张表
- `media` 加 `work_id`（可空 FK）与 `season`（可空）

`media.work_id` 这一步**故意留可空**，`uq_media_identity` 也**故意不动** ——
历史 92 万行还没回填 work_id，现在就收口会让迁移跑不过去；而回填期间仍然
需要旧约束挡住重复插入。收口在迁移 B（`canon rebuild` / `canon merge`
跑完之后）：work_id 设 NOT NULL、加 UNIQUE(work_id, season)、删旧约束。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from funflix.models.base import UTCDateTime

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 和 `models/base.py` 的 `JsonType` 保持一致：PG 上用 JSONB，其余方言用 JSON。
_JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "work",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("norm_key", sa.String(length=500), nullable=False),
        sa.Column("original_title", sa.Text(), nullable=True),
        sa.Column("aliases", _JSON, nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("douban_id", sa.String(length=32), nullable=True),
        sa.Column("imdb_id", sa.String(length=16), nullable=True),
        sa.Column("poster_url", sa.String(length=1024), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("season_count", sa.Integer(), nullable=False),
        sa.Column("resource_count", sa.Integer(), nullable=False),
        sa.Column("valid_resource_count", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work")),
        # Work 的身份**只有 norm_key** —— 不含 year / media_type。
        # 原因见 models/work.py 的模块说明。
        sa.UniqueConstraint("norm_key", name="uq_work_norm_key"),
    )
    with op.batch_alter_table("work", schema=None) as batch_op:
        batch_op.create_index("ix_work_title", ["title"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_tmdb_id"), ["tmdb_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_douban_id"), ["douban_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_imdb_id"), ["imdb_id"], unique=False)

    op.create_table(
        "title_canon",
        sa.Column("norm_key", sa.String(length=500), nullable=False),
        sa.Column("work_norm_key", sa.String(length=500), nullable=True),
        sa.Column("work_title", sa.String(length=500), nullable=True),
        sa.Column("season", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("is_junk", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("decided_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("norm_key", name=op.f("pk_title_canon")),
    )
    with op.batch_alter_table("title_canon", schema=None) as batch_op:
        batch_op.create_index("ix_title_canon_status", ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_title_canon_work_norm_key"), ["work_norm_key"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_title_canon_created_at"), ["created_at"], unique=False)

    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.add_column(sa.Column("work_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("season", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_media_work_id"), ["work_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_media_work_id_work", "work", ["work_id"], ["id"], ondelete="CASCADE"
        )

    if _is_postgres():
        # 搜索改走 Work 之后，模糊匹配打在这两列上。
        # 写法与 `a1b2c3d4e5f6_pg_trgm_search.py` 一致：必须是 `%` 操作符
        # 才能用上 gin_trgm_ops，`similarity()` 函数形式走不了索引。
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_norm_key_trgm "
            "ON work USING gin (norm_key gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_title_trgm ON work USING gin (title gin_trgm_ops)"
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP INDEX IF EXISTS ix_work_title_trgm")
        op.execute("DROP INDEX IF EXISTS ix_work_norm_key_trgm")

    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.drop_constraint("fk_media_work_id_work", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_media_work_id"))
        batch_op.drop_column("season")
        batch_op.drop_column("work_id")

    op.drop_table("title_canon")
    op.drop_table("work")
