"""数据维护：重建流水线数据、重新归类标签。

这些操作会**不可逆地改数据**，所以放在服务层而不是 CLI 命令体里 ——
只存在于 Typer 命令里的算法既测不了，也没法被接口或 worker 复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.models import Base, Source, Tag, TagKind, media_tag
from funflix.services.text.normalize import classify_tag

#: 重建时保留的表。采集源是**配置**，不是采集回来的数据。
PRESERVED_TABLES = frozenset({"source", "alembic_version"})


def data_tables(keep_documents: bool = False) -> list[str]:
    """列出重建时要清空的表，按外键依赖倒序（先删子表）。

    从 ORM 元数据推导，不手工维护清单。曾经这里是一个写死的六元组，
    后来加的 `tag` / `media_tag` 没人记得补进去 —— 结果 `db reset` 之后
    `tag` 行还在、`media_count` 还停在旧值，而 `media` 已经空了；
    重新解析时这些标签被复用，计数从错误的基数上继续累加，一次比一次离谱。

    表是数据库结构的一部分，让结构自己说清楚有哪些表，比让人记得同步一份
    副本可靠得多。
    """
    names = []
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in PRESERVED_TABLES:
            continue
        if keep_documents and table.name == "raw_document":
            continue
        names.append(table.name)
    return names


@dataclass(slots=True)
class ResetReport:
    tables: list[str] = field(default_factory=list)
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    cursors_reset: bool = False


async def _counts(session: AsyncSession, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in tables:
        out[table] = (await session.execute(text(f"select count(*) from {table}"))).scalar() or 0
    return out


async def reset_pipeline_data(
    session: AsyncSession,
    *,
    keep_documents: bool = False,
    keep_cursors: bool = False,
) -> ResetReport:
    """清空流水线数据，保留采集源配置。

    Args:
        keep_documents: 保留原始文本，只重建下游解析结果。
        keep_cursors: 保留采集水位。清空原始文本时**不要**用 ——
            水位还在的话采集器会认为"都采过了"，重建后一条也拉不回来。
    """
    tables = data_tables(keep_documents=keep_documents)
    if keep_documents and not keep_cursors:
        # 原始文本还在，水位归零只会导致重复采集后被 content_hash 挡掉，无意义
        keep_cursors = True

    # 报告覆盖全部数据表，而不只是这次被清空的那些 —— 用了 --keep-documents
    # 的人最想确认的恰恰是"原始文本还在不在"，只报清空的表就看不到它。
    reported = [*data_tables(), "source"]
    report = ResetReport(tables=tables, before=await _counts(session, reported))

    if session.bind.dialect.name == "postgresql":
        await session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    else:
        # SQLite 没有 TRUNCATE，逐表 DELETE。tables 已按依赖倒序，先删子表。
        for table in tables:
            await session.execute(text(f"DELETE FROM {table}"))

    if not keep_cursors:
        for source in await session.scalars(select(Source)):
            source.reset_watermark()

    await session.commit()
    report.after = await _counts(session, reported)
    report.cursors_reset = not keep_cursors
    return report


@dataclass(slots=True)
class RetagReport:
    total: int = 0
    moved: int = 0
    merged: int = 0
    recounted: int = 0


async def retag_all(session: AsyncSession) -> RetagReport:
    """按当前规则重新归类已有标签。

    维度判定规则会迭代（比如题材白名单），但规则只影响**新建**的标签 ——
    改规则前存进去的行不会自己变。这里把历史数据补齐。

    同一个标签名在新旧维度下各有一行时会合并：关联迁到新行，旧行删除。
    """
    report = RetagReport()
    tags = list(await session.scalars(select(Tag)))
    report.total = len(tags)
    # 先建索引，避免每次都查库
    by_identity = {(t.kind.value, t.norm_key): t for t in tags}

    for tag in tags:
        new_kind = classify_tag(tag.name)
        if new_kind == tag.kind.value:
            continue

        target = by_identity.get((new_kind, tag.norm_key))
        if target is None or target.id == tag.id:
            tag.kind = TagKind(new_kind)
            by_identity[(new_kind, tag.norm_key)] = tag
            report.moved += 1
            continue

        # 目标维度下已有同名标签：把关联迁过去再删旧行。
        # 迁移前要剔掉两边都有的作品，否则会撞 (media_id, tag_id) 唯一键。
        dupes = select(media_tag.c.media_id).where(media_tag.c.tag_id == target.id)
        await session.execute(
            update(media_tag)
            .where(media_tag.c.tag_id == tag.id, media_tag.c.media_id.not_in(dupes))
            .values(tag_id=target.id)
        )
        await session.execute(delete(media_tag).where(media_tag.c.tag_id == tag.id))
        await session.delete(tag)
        report.merged += 1

    await session.flush()
    report.recounted = await recount_tags(session)
    await session.commit()
    return report


async def recount_tags(session: AsyncSession) -> int:
    """按关联表重算全部标签的 `media_count`。返回被修正的行数。

    与 `services.counters` 同一个取舍：重算而非增量维护。
    """
    counts = dict(
        (
            await session.execute(
                select(media_tag.c.tag_id, func.count()).group_by(media_tag.c.tag_id)
            )
        ).all()
    )
    fixed = 0
    for tag in await session.scalars(select(Tag)):
        actual = counts.get(tag.id, 0)
        if tag.media_count != actual:
            tag.media_count = actual
            fixed += 1
    return fixed
