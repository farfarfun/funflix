"""pg_trgm 模糊搜索索引

Revision ID: a1b2c3d4e5f6
Revises: 9036e3fe2a7e
Create Date: 2026-08-26

只在 PostgreSQL 上生效。SQLite 没有 pg_trgm，搜索回落到 LIKE 后端，
schema 保持不变 —— 这样同一份迁移在两种方言上都能跑通。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "9036e3fe2a7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # 扩展可能已由 DBA 装过，IF NOT EXISTS 保证幂等
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # GIN + gin_trgm_ops 才能让 similarity() 和 LIKE '%x%' 走索引。
    # 没有它，模糊查询每次都是全表扫描 —— 几万行时就是秒级变几十秒。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_media_norm_key_trgm "
        "ON media USING gin (norm_key gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_media_title_trgm "
        "ON media USING gin (title gin_trgm_ops)"
    )
    # 标签名也要能模糊搜
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tag_name_trgm ON tag USING gin (name gin_trgm_ops)"
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP INDEX IF EXISTS ix_tag_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_media_title_trgm")
    op.execute("DROP INDEX IF EXISTS ix_media_norm_key_trgm")
    # 扩展不删 —— 可能有其它表在用
