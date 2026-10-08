"""阶段 1：删掉根本不是作品的 media 行。

生产库里有十几万行 media 的"标题"是采集残渣，而不是片名：网盘按钮文案
（`夸克` 45,888 行、`查看资源` 6,177、`磁力下载` 5,636）、表格列名、
行号、漏进来的分享 ID、整条磁力链、网盘客户端安装包。

这些行必须**先删**再归并，否则它们会在后面每一步里继续污染：
`series_norm_key` 给每一条算出一个独一无二的键，于是 Work 表里凭空多出
十几万个垃圾作品；LLM 分块时它们各自占一个块，白付钱。

**resource 行不删** —— 链接本身是真的，只是归属错了。删掉关联之后它们
变成未归属资源，等后续富化或人工归位。这也是为什么这一步只动 media 侧的
三张表，碰不到 `resource` / `link_check` / `raw_document`（那些是真实采集
成本，197 万 / 211 万行）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from farlog import getLogger
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.dbconflict import is_write_conflict, retry_on_write_conflict
from funflix.models import Media, Tag, media_resource, media_tag
from funflix.services.counters import lock_tags_in_order, refresh_work_counters
from funflix.services.text.normalize import clean_title, looks_like_junk_title, series_norm_key

logger = getLogger("funflix")

#: 每批删多少行 media。远端 PG 一次往返 ~100ms，批太小会被往返吃掉；
#: 批太大则单个事务持锁过久，中断时白做的工作也多。
#: 500 是 `maintenance._chunks` 的默认值，沿用。
CHUNK = 500

#: 每批重算多少个标签的 `media_count`。比 `CHUNK` 小得多，因为这里每行的
#: 代价极不均匀：相关子查询要给「夸克」数 45,888 行、给冷门标签数 3 行，
#: 而全库总共才一千多个标签 —— 200 已经能分成好几批，把最热那几把行锁的
#: 持有窗口压到一条语句之内。见 `_recount_tags`。
TAG_RECOUNT_CHUNK = 200

#: 留几条样例给人核对。只是展示用，不影响统计。
SAMPLE_LIMIT = 40


@dataclass(slots=True)
class PurgeReport:
    scanned: int = 0
    junk: int = 0
    deleted: int = 0
    #: 因为所属 media 被删而变成未归属的 resource 关联数。
    #: 这是**关联数**不是 resource 行数 —— 一条链接可能挂在多部作品上
    #: （合集），其中一部是垃圾另一部是真作品的情况确实存在。
    links_detached: int = 0
    tags_detached: int = 0
    tags_recounted: int = 0
    #: 撞车重试耗尽、`media_count` 暂时停在旧值的标签数。见 `_recount_tags`。
    tags_recount_abandoned: int = 0
    #: 因为丢了季而被重算的 Work 数。`canon purge` 在 rebuild 之前跑时恒为 0
    #: （那时还没有 Work），重跑在 rebuild 之后才有值。
    works_recounted: int = 0
    dry_run: bool = True
    samples: list[str] = field(default_factory=list)


def is_junk_media_title(title: str | None) -> bool:
    """判定一行 media 该不该删。

    入参是 `media.title` —— 也就是**旧规则**的清洗产出，不是原始分享文本。
    这是刻意的：原始文本在 `resource.title_raw` 上，而一行 media 挂着几十条
    resource，哪一条的原始标题算数没有定论。原地重洗 `media.title` 是
    幂等的（`clean_title` 的每一步都是"剥掉噪声"，对已经干净的标题是空操作），
    而且与模拟时的测算口径一致。

    代价是旧规则**洗坏**的行恢复不了：`_SCRAPE_LABEL_RE` 曾经把
    `导演万岁` 剥成 `万岁`，那个「导演」找不回来了。这类行不会被误删
    （`万岁` 不是垃圾键），结局是当成另一部作品单独成行。
    """
    return looks_like_junk_title(clean_title(title or ""))


async def _recount_tags(session: AsyncSession, tag_ids: set[uuid.UUID]) -> tuple[int, int]:
    """按关联表重算指定标签的 `media_count`。返回 `(重算行数, 放弃行数)`。

    与 `services.counters` 同一个取舍：重算而非增减。这里只重算**受影响**的
    标签，不是全表 —— 垃圾行上的标签大多是 `夸克` 这类网盘名，集中在少数
    几个标签上，全表重算（`maintenance.recount_tags`）要把整个 media_tag 扫一遍。

    **分批、按批提交、每批自己扛死锁**，三件事缺一不可，教训同
    `maintenance._flush_relink_batch`：

    原先是一条 UPDATE 把全部受影响标签一次写完。那条语句给每个标签跑一次
    `count(*)` 相关子查询（光「夸克」就要数 45,888 行 media_tag），于是它
    一路持着全库最热那几把行锁跑好几秒，而 CI 里 parse 八个分片正在同一批
    行上写增量。实测结果是 run 37841925687 的 Merge 步撞上三方死锁、整个
    canon job 退出 1 —— 连它后面那段 Work 计数重算（`delete_media_rows`
    末尾的循环）都没跑到，库里留下一批计数失真的 Work。

    放弃一批只是让这些标签的 `media_count` 暂时停在旧值（排序用的冗余列，
    `maintenance.recount_tags` 能全表补回来），比让异常冒到命令层划算得多。
    """
    if not tag_ids:
        return 0, 0
    actual = (
        select(func.count())
        .select_from(media_tag)
        .where(media_tag.c.tag_id == Tag.id)
        .scalar_subquery()
    )
    recounted = 0
    abandoned = 0
    ordered = sorted(tag_ids)
    for start in range(0, len(ordered), TAG_RECOUNT_CHUNK):
        batch = ordered[start : start + TAG_RECOUNT_CHUNK]

        async def _once(batch: list[uuid.UUID] = batch) -> int:
            # SAVEPOINT 里做：死锁会把事务打进 aborted，不回滚的话后面每一批
            # 都报「current transaction is aborted」，一次撞车废掉整轮。
            async with session.begin_nested():
                await lock_tags_in_order(session, batch)
                result = await session.execute(
                    update(Tag).where(Tag.id.in_(batch)).values(media_count=actual)
                )
            await session.commit()
            return result.rowcount or 0

        try:
            recounted += await retry_on_write_conflict(_once, what=f"标签计数这批 {len(batch)} 行")
        except DBAPIError as err:
            if not is_write_conflict(err):
                raise
            logger.warning(f"标签计数这批 {len(batch)} 行撞车重试耗尽，放弃（留给全表重算）")
            abandoned += len(batch)
    return recounted, abandoned


@dataclass(slots=True)
class _BatchStats:
    links: int = 0
    tags: int = 0
    affected_tags: set[uuid.UUID] = field(default_factory=set)
    affected_works: set[uuid.UUID] = field(default_factory=set)


async def _delete_batch(session: AsyncSession, media_ids: list[uuid.UUID]) -> _BatchStats:
    """删掉一批 media 及其关联。

    两张关联表上都有 `ON DELETE CASCADE`，光删 media 行数据也是对的。
    这里仍然显式先删子表，为的是三件 CASCADE 给不了的东西：被断开的关联
    **条数**（报告要用）、受影响的 **tag_id**、以及受影响的 **work_id**
    （两者都要重算计数）。顺手也不依赖 SQLite 的 `PRAGMA foreign_keys` 开着。
    """
    stats = _BatchStats()
    stats.affected_tags = set(
        await session.scalars(select(media_tag.c.tag_id).where(media_tag.c.media_id.in_(media_ids)))
    )
    # 归属也要在删之前问清楚 —— 行没了就查不到它曾经属于哪部作品了。
    # 过滤 NULL：这一步在迁移 B 之前也能跑（`canon purge` 正是在 rebuild
    # **之前**跑的，那时每一行的 work_id 都还是空的）。
    stats.affected_works = {
        work_id
        for work_id in await session.scalars(select(Media.work_id).where(Media.id.in_(media_ids)))
        if work_id is not None
    }
    links = await session.execute(
        delete(media_resource).where(media_resource.c.media_id.in_(media_ids))
    )
    tags = await session.execute(delete(media_tag).where(media_tag.c.media_id.in_(media_ids)))
    await session.execute(delete(Media).where(Media.id.in_(media_ids)))
    stats.links = links.rowcount or 0
    stats.tags = tags.rowcount or 0
    return stats


@dataclass(slots=True)
class DeleteStats:
    deleted: int = 0
    links_detached: int = 0
    tags_detached: int = 0
    tags_recounted: int = 0
    #: 撞车重试耗尽、`media_count` 暂时停在旧值的标签数。见 `_recount_tags`。
    tags_recount_abandoned: int = 0
    #: 因为丢了季而被重算的 Work 数。
    works_recounted: int = 0


async def delete_media_rows(
    session: AsyncSession,
    media_ids: list[uuid.UUID],
    *,
    on_progress: Callable[[int], None] | None = None,
) -> DeleteStats:
    """分批删掉指定的 media 行，并修正标签与作品计数。按批提交。

    阶段 4（`apply`）也走这条路：LLM 判出来的 junk 和规则判出来的 junk
    该有完全一样的删除语义 —— 同样保留 resource 行、同样重算受影响的标签。

    **计数收尾是这个函数的职责**，不是调用方的。删掉一行 media 会让它所属
    Work 的 `season_count` / `resource_count` 当场失真，而失真的 Work 从
    media 侧已经查不出来了（行没了）。所以这里和标签一样，在删之前记下归属、
    删完顺手重算 —— 漏掉的话库里会留下一批「声称有 1 季、实际 0 季」的
    Work（`#大主宰 #leoziyuan #动画` 那条垃圾就是这么跑出来的）。

    空 Work 本身**不删**，只把计数刷成 0 —— 与 `refresh_work_counters` 的
    契约一致，理由见 `canon/apply.py` 开头「空出来的 Work 不删」。
    """
    stats = DeleteStats()
    affected_tags: set[uuid.UUID] = set()
    affected_works: set[uuid.UUID] = set()
    for start in range(0, len(media_ids), CHUNK):
        batch = media_ids[start : start + CHUNK]
        batch_stats = await _delete_batch(session, batch)
        stats.links_detached += batch_stats.links
        stats.tags_detached += batch_stats.tags
        stats.deleted += len(batch)
        affected_tags |= batch_stats.affected_tags
        affected_works |= batch_stats.affected_works
        await session.commit()
        if on_progress is not None:
            on_progress(stats.deleted)

    stats.tags_recounted, stats.tags_recount_abandoned = await _recount_tags(session, affected_tags)

    works = sorted(affected_works)
    for start in range(0, len(works), CHUNK):
        stats.works_recounted += await refresh_work_counters(session, works[start : start + CHUNK])
        await session.commit()
    return stats


async def _scan(
    session: AsyncSession, report: PurgeReport, key: str | None, limit: int | None
) -> list[uuid.UUID]:
    """只读地扫一遍，挑出要删的 media id。

    **扫描和删除必须分两趟** —— 不能边 stream 边 commit。`stream()` 的游标
    活在当前事务里，循环体内 `commit()` 会把它连根拔掉，后续迭代直接报错。
    先攒 id 再分批删，内存代价也可以接受：12 万个 UUID 不到 10 MB。
    """
    victims: list[uuid.UUID] = []
    rows = await session.stream(select(Media.id, Media.title))
    async for media_id, title in rows:
        report.scanned += 1
        if key is not None and series_norm_key(title or "") != key:
            continue
        if not is_junk_media_title(title):
            continue

        report.junk += 1
        if len(report.samples) < SAMPLE_LIMIT:
            report.samples.append(title or "")
        if limit is None or len(victims) < limit:
            victims.append(media_id)
    return victims


async def purge_junk_media(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    key: str | None = None,
    limit: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> PurgeReport:
    """扫全表，删掉 `is_junk_media_title` 命中的 media 行。

    Args:
        dry_run: 只统计和抽样，不写库。**默认开** —— 这是个不可逆的批量删除。
        key: 只处理 `series_norm_key` 等于它的行。用于单组演练（`--key 大主宰`
            先确认这一组的判定是对的，再放开全库）。过滤只能在 Python 侧做：
            `series_norm_key` 是纯函数，库里没有这一列 —— 它正是这次改造
            要算出来的东西。
        limit: 最多删多少行。配合 `dry_run=False` 做小步试探。
            注意 `report.junk` 仍然是全表命中数，`report.deleted` 才受它限制。
        on_progress: 每提交一批调一次，入参是累计删除行数。

    按批提交。中断后重跑只会把剩下的删掉 —— 判定是纯函数，已删的行不会再
    出现，所以重跑幂等。
    """
    report = PurgeReport(dry_run=dry_run)
    victims = await _scan(session, report, key, limit)
    if dry_run:
        return report

    stats = await delete_media_rows(session, victims, on_progress=on_progress)
    report.deleted = stats.deleted
    report.links_detached = stats.links_detached
    report.tags_detached = stats.tags_detached
    report.tags_recounted = stats.tags_recounted
    report.tags_recount_abandoned = stats.tags_recount_abandoned
    report.works_recounted = stats.works_recounted
    return report
