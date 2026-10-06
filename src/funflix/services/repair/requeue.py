"""深层修复：把规则集版本过期的文档打回解析队列。

浅层修复（`scan.py` / `apply.py`）只能从已清洗的 `media.title` 重算，所以修
不了两类情况：旧规则**洗坏**、信息已经丢了的行（`_SCRAPE_LABEL_RE` 曾把
`导演万岁` 剥成 `万岁`，那个「导演」找不回来了），以及抽取器**切分逻辑**本身
变了的情况（一条分享该拆成几个作品项变了，这在 media 层面根本看不出来）。

这两类只能回到 `raw_document.content` 重解析。

## 为什么要版本戳做预筛

生产库有 213 万份文档。每次改规则都全量重跑的话，这个节点就只能偶尔手动跑
一次，而不是挂在流水线上 —— 那正好和「规则一直在加」的现实相反。

所以 `raw_document.parse_rules_version` 记下「这一行是用哪一版规则解析的」，
这里只捞版本对不上的。详见 `models/raw.py` 的 `PARSE_RULES_VERSION`
（手工 bump，刻意不用源码哈希）。

## 为什么没有执行器

打回 `pending` 之后**什么都不用做** —— 现有的 parse 节点本来就是个队列，
会按它自己的 `--limit` 节奏消化。深层修复因此天然自限速：这里按 `--limit`
每轮放一批进队列，parse 那边按自己的速度磨。两个节点互不等待、互不冲突。

## 只打回已完成的

`parse_status` 是 `pending` / `failed` 的文档本来就在队列里（或在退避里），
碰它只会把 `parse_attempts` 和退避时间清掉，让一份一直失败的文档重新开始
无意义的重试。所以只收 `DONE`。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import ParseStatus
from funflix.models.raw import PARSE_RULES_VERSION, RawDocument


@dataclass(slots=True)
class RequeueReport:
    #: 版本对不上的已解析文档总数（不受 `limit` 影响）。
    stale: int = 0
    #: 这一轮真的打回队列的数量。
    requeued: int = 0
    version: str = PARSE_RULES_VERSION
    dry_run: bool = True


def _stale_filter() -> ColumnElement[bool]:
    """「已解析完，但规则版本对不上」。

    `NULL` 也算对不上 —— 那是版本戳这个机制上线之前解析的行。用
    `IS DISTINCT FROM` 会在 SQLite 上挂（不支持），所以显式写成
    `IS NULL OR != current`，两个方言都能走
    `ix_raw_document_rules_version`。
    """
    return (RawDocument.parse_rules_version.is_(None)) | (
        RawDocument.parse_rules_version != PARSE_RULES_VERSION
    )


async def requeue_stale_documents(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    limit: int | None = None,
) -> RequeueReport:
    """把规则版本过期的已解析文档打回 `pending`。

    Args:
        dry_run: 只统计，不写库。**默认开**。
        limit: 这一轮最多打回多少份。

    `parse_attempts` / `lease_until` / `next_parse_at` / `parse_error` 一并清掉：
    不清的话，一份历史上失败过几次的文档会带着旧的退避时间重新进队列，
    可能几小时内都捞不起来 —— 而它这次是因为规则变了才重排的，和上次的
    失败没有关系。
    """
    report = RequeueReport(dry_run=dry_run)
    done = RawDocument.parse_status == ParseStatus.DONE
    report.stale = (
        await session.scalar(
            select(func.count()).select_from(RawDocument).where(done, _stale_filter())
        )
        or 0
    )
    if dry_run:
        # 报出「这一轮**会**打回多少」，而不是 0 —— dry-run 的用处就是让人
        # 先看清这一轮的量级再决定要不要放行。
        report.requeued = report.stale if limit is None else min(report.stale, limit)
        return report
    if report.stale == 0:
        return report

    # 子查询选 id 再 UPDATE ... WHERE id IN —— PG 不支持
    # `UPDATE ... LIMIT`，而这里必须能限额（一次把 213 万份全打回队列，
    # parse 节点要刷好几天，中间所有搜索结果都在退化）。
    picked = select(RawDocument.id).where(done, _stale_filter())
    if limit is not None:
        picked = picked.limit(limit)
    # 用 `RETURNING` 数，而不是 `rowcount`：PG 和 SQLite 都支持，而且数出来的
    # 是**确定**改了的那些行 —— 不像 rowcount，它在不同驱动下对
    # 「匹配到但值没变」的计法不一致。
    changed = await session.scalars(
        update(RawDocument)
        .where(RawDocument.id.in_(picked.scalar_subquery()))
        .values(
            parse_status=ParseStatus.PENDING,
            parse_attempts=0,
            parse_error=None,
            lease_until=None,
            next_parse_at=None,
        )
        .returning(RawDocument.id)
        .execution_options(synchronize_session=False)
    )
    report.requeued = len(changed.all())
    await session.commit()
    return report
