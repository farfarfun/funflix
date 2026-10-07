"""给批量解析的翻页路径补索引：(parse_status, last_parsed_at NULLS FIRST, id)

Revision ID: e5f6a7b8c9d0
Revises: c5d6e7f8a9b0
Create Date: 2026-10-06

## 为什么要这条索引

`concurrent_runner.py` 的生产者按 `(last_parsed_at NULLS FIRST, id)` 有序
keyset 翻页（`runner.py::keyset_after`）。既有的 `ix_raw_document_parse_queue`
是 `(parse_status, next_parse_at)`，第二列不对，支撑不了这个排序。

生产库实测（213 万行待解析，`EXPLAIN (ANALYZE, BUFFERS)` 翻一页 500 条）：

    Limit (actual time=771.972..773.493 rows=500)
      -> Gather Merge
        -> Sort (Sort Key: last_parsed_at NULLS FIRST, id)
          -> Parallel Seq Scan on raw_document (rows=709510 loops=3)
    Buffers: shared hit=116259 read=333964      -- 约 2.6GB
    Execution Time: 773.556 ms

**每翻一页都重扫一次全表**。单进程跑时这被一页 500 条摊薄成约 1.5ms/条、
相对每条约 270ms 的落库往返不值一提，所以一直没暴露；但 `funflix parse
--shard i/N` 多进程分片一上来，就是 N 路并发全表扫，I/O 直接打满。

## 为什么是 PostgreSQL 专属

索引里的 NULL 方向必须和查询的 `NULLS FIRST` 一致，否则 PG 拿到也得重排、
白建。而 SQLite 虽然支持 `ORDER BY ... NULLS FIRST`，却不支持建索引时写它
（3.50.4 实测报 `unsupported use of NULLS FIRST`）。

单测库只有几十行，走不走索引没有区别，所以这里按方言跳过——而不是反过来
改查询的排序语义去迁就 SQLite。ORM 侧对应 `models/raw.py` 里那条
`.ddl_if(dialect="postgresql")`，两边要一起改。

## 为什么用 CONCURRENTLY

目标表 213 万行、2.6GB，而且重建期间 `funflix parse` 正在往它写
`parse_status`/`last_parsed_at`。普通 `CREATE INDEX` 会拿 SHARE 锁把写入堵住
整个建索引的时长；`CONCURRENTLY` 不阻塞 DML，代价是不能跑在事务里，所以套了
`autocommit_block()`。

副作用是它**可能留下无效索引**（建到一半失败时索引还在、但 `indisvalid=false`，
查询用不上也不会报错）。真遇到就先 `DROP INDEX ix_raw_document_parse_scan`
再重跑本迁移，而不是以为迁移成功了却还在全表扫。查法：

    SELECT indexrelid::regclass, indisvalid
      FROM pg_index WHERE indexrelid = 'ix_raw_document_parse_scan'::regclass;
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_raw_document_parse_scan"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        # SQLite 建不出带 NULLS FIRST 的索引，见模块 docstring。
        return
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            "ON raw_document (parse_status, last_parsed_at NULLS FIRST, id)"
        )


def downgrade() -> None:
    if not _is_postgresql():
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
