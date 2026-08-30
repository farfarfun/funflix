"""跨库同步：`pull`（remote → local，remote 为准）/ `push`（local → remote）。

不引入水位表——水位现查 `MAX(watermark_column)`，配合按主键的 upsert 语义，
把"两边内容逐步趋同"建立在幂等操作上，不用额外维护一份同步状态。

`ON CONFLICT` 只保护主键；六张表在主键之外还各自有一个业务唯一键
（`resource.(provider, share_id)`、`source.(source_type, identifier)`、
`raw_document.content_hash` 等）。如果推送的行主键在对端是新的、但业务键已经
存在（比如 `funflix serve` 直连远端摄入的内容和本地 collect 刚采到的一样，走
的是两条独立路径生成了不同的 uuid7），批量 upsert 会在业务唯一键上报
`IntegrityError`——这时降级为逐行处理，单行冲突就跳过并记录警告，不拖累整批
里其余没问题的行。这一行会留在"未同步"状态，下一轮 pull 会看到对端的权威版
本；孤儿行不会造成数据损坏，只是不会被自动清理。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.models import Base
from funflix.services.sync.tables import SyncTable, sync_tables

logger = logging.getLogger(__name__)

#: 容忍两台机器的时钟漂移，以及"提交时刻"与"对其他连接可见时刻"之间的偏差。
#: 多拉到的重叠部分靠 upsert 的幂等性兜底，不会造成数据错误，只是多扫一遍。
_WATERMARK_OVERLAP = timedelta(minutes=5)

#: 每批 upsert 的行数上限，避免一条语句塞进去几万行。
_CHUNK_SIZE = 500


@dataclass(slots=True)
class TableSyncResult:
    table: str
    fetched: int = 0
    applied: int = 0
    skipped_conflicts: int = 0


@dataclass(slots=True)
class SyncReport:
    tables: list[TableSyncResult] = field(default_factory=list)

    @property
    def total_applied(self) -> int:
        return sum(t.applied for t in self.tables)

    @property
    def total_skipped(self) -> int:
        return sum(t.skipped_conflicts for t in self.tables)


def _insert_stmt(session: AsyncSession, table: sa.Table) -> postgresql.Insert | sqlite.Insert:
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        return postgresql.insert(table)
    if dialect == "sqlite":
        return sqlite.insert(table)
    raise NotImplementedError(f"同步不支持方言: {dialect}")


def _upsert_stmt(
    session: AsyncSession, spec: SyncTable, values: list[dict]
) -> postgresql.Insert | sqlite.Insert:
    pk_cols = [c.name for c in spec.table.primary_key.columns]
    update_cols = [c.name for c in spec.table.columns if c.name not in pk_cols]

    stmt = _insert_stmt(session, spec.table).values(values)
    if spec.mutable and update_cols:
        wm = spec.watermark_column
        stmt = stmt.on_conflict_do_update(
            index_elements=pk_cols,
            set_={c: getattr(stmt.excluded, c) for c in update_cols},
            where=spec.table.c[wm] < stmt.excluded[wm],
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
    return stmt


async def _max_watermark(session: AsyncSession, spec: SyncTable):
    return await session.scalar(sa.select(sa.func.max(spec.table.c[spec.watermark_column])))


async def _fetch_rows(session: AsyncSession, spec: SyncTable, since) -> list[dict]:
    stmt = sa.select(spec.table)
    if since is not None:
        stmt = stmt.where(spec.table.c[spec.watermark_column] > since)
    rows = (await session.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def _apply_rows(session: AsyncSession, spec: SyncTable, rows: list[dict]) -> TableSyncResult:
    result = TableSyncResult(table=spec.table.name, fetched=len(rows))
    if not rows:
        return result

    pk_cols = [c.name for c in spec.table.primary_key.columns]

    for start in range(0, len(rows), _CHUNK_SIZE):
        chunk = rows[start : start + _CHUNK_SIZE]
        try:
            async with session.begin_nested():
                await session.execute(_upsert_stmt(session, spec, chunk))
            result.applied += len(chunk)
            continue
        except DBAPIError:
            # 覆盖唯一键冲突，也覆盖数据不合法（比如超长字段撞上 varchar 长度）——
            # 本地 SQLite 不强制这些约束，问题字段能顺利落进本地库，直到 push
            # 到远端 Postgres 才会报错。catch 用基类 DBAPIError 而不是具体的
            # IntegrityError/DataError：asyncpg 的错误映射表并不完整（见
            # sqlalchemy.dialects.postgresql.asyncpg._asyncpg_error_translate），
            # 像 StringDataRightTruncationError 这类真实会遇到的错误最终只会
            # 被包成通用的 DBAPIError，具体子类反而抓不到。
            logger.warning(
                "%s: 批量 upsert 撞上业务唯一键冲突或数据不合法，降级为逐行处理（%d 行）",
                spec.table.name,
                len(chunk),
            )

        for row in chunk:
            try:
                async with session.begin_nested():
                    await session.execute(_upsert_stmt(session, spec, [row]))
                result.applied += 1
            except DBAPIError:
                pk_value = {c: row.get(c) for c in pk_cols}
                logger.warning(
                    "%s: 跳过一行（唯一键冲突或数据不合法），主键=%s", spec.table.name, pk_value
                )
                result.skipped_conflicts += 1

    await session.commit()
    return result


async def _sync_direction(source: AsyncSession, target: AsyncSession) -> SyncReport:
    report = SyncReport()
    for spec in sync_tables():
        target_max = await _max_watermark(target, spec)
        since = target_max - _WATERMARK_OVERLAP if target_max is not None else None
        rows = await _fetch_rows(source, spec, since)
        report.tables.append(await _apply_rows(target, spec, rows))
    return report


async def ensure_local_schema(local: AsyncSession) -> None:
    """CI runner 上的本地 SQLite 文件可能是全新的、还没有任何表。

    不借道 alembic 建表——迁移脚本里有 Postgres-only 的 DDL（pg_trgm 扩展、
    列物理重排），本地库既然是 SQLite 就跑不通。直接用 ORM metadata
    建表：`CREATE TABLE IF NOT EXISTS` 语义，表已存在时是空操作。
    """
    conn = await local.connection()
    await conn.run_sync(Base.metadata.create_all)


async def pull(local: AsyncSession, remote: AsyncSession) -> SyncReport:
    """remote → local，remote 为准。按外键依赖顺序（父表先）逐表处理。"""
    await ensure_local_schema(local)
    return await _sync_direction(source=remote, target=local)


async def push(local: AsyncSession, remote: AsyncSession) -> SyncReport:
    """local → remote。按外键依赖顺序（父表先）逐表处理。"""
    await ensure_local_schema(local)
    return await _sync_direction(source=local, target=remote)
