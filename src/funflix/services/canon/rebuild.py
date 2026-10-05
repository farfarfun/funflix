"""阶段 2：按升级后的规则重算键，确定性地建 Work 并回填 media 的归属。

**零 token**。实测把 92.9 万行 media 收敛到 44 万个 Work —— 也就是说光靠
规则就搬掉了绝大部分重复，LLM 只需要打剩下的残局（约 4,900 个块）。

分三趟走，每一趟都有它必须单独存在的理由：

1. **扫描**（只读）—— 算出每行的 `series_norm_key`，按键聚合出作品级属性。
   不能和回填合在一趟：作品属性（展示名、首播年、类型）要看完全组才能定，
   而回填需要作品的 id，鸡生蛋。
2. **建 Work** —— 把新键插成 Work 行。
3. **回填** —— 写 media 的 `work_id` / `season`，顺带把 `title` 按新规则刷一遍。
   `norm_key` **不刷**，理由见 `_assign` 里的注释。

早先这里还有第 4 趟「并季」：先把所有行搬到目标身份，再 `GROUP BY ... HAVING
count(*) > 1` 找出撞车的并掉。那套写法依赖「`uq_media_season` 还没加上」这个
前提，于是 rebuild 变成了只能在迁移 B 之前跑一次的一次性工具。
现在搬迁和合并合成一步（`canon/assign.py`），任何时刻库里都不存在重复的
`(work_id, season)`，rebuild 在收口之后也能照常重跑 —— 改了归一规则想重新
分组是个正常需求。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import MediaType
from funflix.models import Media, Work
from funflix.models.canon import CanonState, TitleCanon
from funflix.models.media import NO_SEASON, UNKNOWN_YEAR
from funflix.services.canon.assign import assign_identities
from funflix.services.canon.purge import is_junk_media_title
from funflix.services.counters import refresh_media_counters, refresh_work_counters
from funflix.services.text.normalize import (
    clean_title,
    extract_season,
    series_norm_key,
    strip_season,
)

CHUNK = 500


@dataclass(slots=True)
class _Agg:
    """一个 `series_norm_key` 下的作品级属性候选，边扫边收。

    刻意只存标量，不存成员 id 列表 —— 44 万个键如果各自挂一个 id 列表，
    等于把整张 media 表的主键读进内存。回填那一趟会再扫一遍，
    那时候按键查 Work 的 id 就够了。
    """

    #: 展示名候选。按「出现次数」投票，次数相同取较短的。
    titles: dict[str, int] = field(default_factory=dict)
    media_type: MediaType = MediaType.UNKNOWN
    year: int = UNKNOWN_YEAR
    rows: int = 0

    def observe(self, title: str, media_type: MediaType, year: int) -> None:
        self.rows += 1
        if title:
            self.titles[title] = self.titles.get(title, 0) + 1
        if self.media_type is MediaType.UNKNOWN and media_type is not MediaType.UNKNOWN:
            # 先到先得，不做多数票。类型分歧（anime vs tv）是 LLM 的活，
            # 这里挑一个只是给个能用的初值。
            self.media_type = media_type
        if year != UNKNOWN_YEAR and (self.year == UNKNOWN_YEAR or year < self.year):
            # 取最小的非零年份：Work 是整个系列，系列的年份是首播年。
            self.year = year

    def best_title(self) -> str:
        """投票选展示名。

        票数相同时取**较短**的那个 —— 同一部剧的写法里，短的那个通常是
        剥得更干净的（`大主宰` vs `大主宰 动漫版`）。再同就按字典序，
        保证重跑结果一致。
        """
        if not self.titles:
            return ""
        return max(self.titles.items(), key=lambda kv: (kv[1], -len(kv[0]), kv[0]))[0]


@dataclass(slots=True)
class RebuildReport:
    scanned: int = 0
    skipped_junk: int = 0
    #: 因为已有裁决而被跳过的行数。首次跑恒为 0（那时 `title_canon` 还是空的），
    #: 重跑时才有值 —— 见 `rebuild_works` 的说明。
    skipped_decided: int = 0
    keys: int = 0
    works_created: int = 0
    works_existing: int = 0
    media_updated: int = 0
    season_conflicts: int = 0
    media_merged: int = 0
    links_moved: int = 0
    links_dropped: int = 0
    #: 为了解开换位冲突而临时挪动的次数，见 `canon/assign.py`。
    parked: int = 0
    works_recounted: int = 0
    dry_run: bool = True
    samples: list[str] = field(default_factory=list)


async def _decided_keys(session: AsyncSession) -> set[str]:
    """已经有裁决的归一键 —— 这些键归 `apply` 管，规则不许再动。

    只认 `decided`：`pending` 是"还没判"、`rejected` 是"判了但被校验丢掉"，
    两种都该继续吃规则的默认分组。
    """
    return set(
        await session.scalars(
            select(TitleCanon.norm_key).where(TitleCanon.status == CanonState.DECIDED)
        )
    )


async def _scan(
    session: AsyncSession, report: RebuildReport, key: str | None, decided: set[str]
) -> dict[str, _Agg]:
    """只读地扫一遍 media，按 `series_norm_key` 聚出作品级属性。"""
    aggs: dict[str, _Agg] = {}
    rows = await session.stream(
        select(Media.title, Media.media_type, Media.year).execution_options(yield_per=2000)
    )
    async for title, media_type, year in rows:
        report.scanned += 1
        if is_junk_media_title(title):
            # 正常流程里 `canon purge` 已经删掉了这些行。这里再挡一次，
            # 是为了让 rebuild 在**没跑过 purge** 的库上也不会凭空造出
            # 十几万个垃圾 Work（比如只想做单组演练的时候）。
            report.skipped_junk += 1
            continue
        cleaned = clean_title(title or "")
        series_key = series_norm_key(title or "")
        if not series_key or (key is not None and series_key != key):
            continue
        if series_key in decided:
            # 这个键已经被裁决过了，规则的意见作废 —— 见 `rebuild_works` 的
            # 说明。不进 `aggs` 就不会有 work_id，`_assign` 也就碰不到这些行。
            report.skipped_decided += 1
            continue
        aggs.setdefault(series_key, _Agg()).observe(
            strip_season(cleaned), media_type, year or UNKNOWN_YEAR
        )

    report.keys = len(aggs)
    for series_key in sorted(aggs, key=lambda k: -aggs[k].rows)[:20]:
        report.samples.append(f"{aggs[series_key].rows:>5} 行  {aggs[series_key].best_title()}")
    return aggs


async def _ensure_works(
    session: AsyncSession, aggs: dict[str, _Agg], report: RebuildReport
) -> dict[str, uuid.UUID]:
    """为每个键 get-or-create 一个 Work，返回 键 → work_id。

    已存在的 Work **不覆盖属性** —— 这一步可能是在重跑，也可能在 LLM 裁决
    之后又跑了一次，那些场景下库里的值比这里按规则猜的更可信。
    """
    work_ids: dict[str, uuid.UUID] = {}
    keys = list(aggs)
    for start in range(0, len(keys), CHUNK):
        batch = keys[start : start + CHUNK]
        existing = dict(
            (
                await session.execute(
                    select(Work.norm_key, Work.id).where(Work.norm_key.in_(batch))
                )
            ).all()
        )
        work_ids.update(existing)
        report.works_existing += len(existing)

        fresh = [
            Work(
                title=aggs[k].best_title()[:500] or k[:500],
                norm_key=k,
                aliases=[],
                media_type=aggs[k].media_type,
                year=aggs[k].year,
            )
            for k in batch
            if k not in existing
        ]
        if fresh:
            session.add_all(fresh)
            await session.flush()
            work_ids.update({w.norm_key: w.id for w in fresh})
            report.works_created += len(fresh)
        await session.commit()
    return work_ids


async def _assign(
    session: AsyncSession,
    work_ids: dict[str, uuid.UUID],
    report: RebuildReport,
    key: str | None,
    on_progress: Callable[[int], None] | None,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """回填 media 的 `work_id` / `season`，并把 `title` 刷成新规则的产出。

    返回 (被动到的 work_id, 吸收了别人关联的存活 media id)，留给收尾重算计数。

    **按主键翻页，不用 `stream()`** —— 这一趟要边读边写边提交，而
    `stream()` 的游标活在当前事务里，循环体内一 `commit()` 就连根拔掉。
    攒完整张表再统一写也不行：80 万条待更新的 dict 在内存里是几百 MB，
    而且会变成一个巨型事务，中断后全部白做。
    按 `id` 翻页天然走主键索引，每页独立提交，中断后重跑是幂等的
    （同样的标题算出同样的键）。

    搬迁交给 `assign_identities`，它搬的同时就把撞到同一个
    `(work_id, season)` 的行并掉 —— 不能先一条 UPDATE 全搬完再回头并，
    那样中途就撞 `uq_media_season` 了（详见 `canon/assign.py`）。
    """
    touched: set[uuid.UUID] = set()
    survivors: set[uuid.UUID] = set()
    cursor: uuid.UUID | None = None

    while True:
        query = select(Media.id, Media.title).order_by(Media.id).limit(CHUNK)
        if cursor is not None:
            query = query.where(Media.id > cursor)
        page = (await session.execute(query)).all()
        if not page:
            break
        cursor = page[-1][0]

        targets: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
        titles: dict[uuid.UUID, str] = {}
        for media_id, title in page:
            series_key = series_norm_key(title or "")
            work_id = work_ids.get(series_key)
            if work_id is None or (key is not None and series_key != key):
                continue
            # `extract_season` 拿不准时返回 None，落 `NO_SEASON`。
            # 这不是"第 0 季"，是"这一行还没分出季" —— 真实季号由 LLM 在
            # 阶段 3 补，所以大多数行会先堆在 season=0 上，再被阶段 4 搬开。
            targets[media_id] = (work_id, extract_season(title or "") or NO_SEASON)
            titles[media_id] = clean_title(title or "")[:500]
            # **`norm_key` 刻意不动**，哪怕迁移 B 已经把 `uq_media_identity`
            # 删掉了。改造完成后 media 的身份是 `(work_id, season)`，这一列
            # 退化成展示/排查用；而把它刷成作品键是 `_upsert_media` 的职责
            # （新入库的行走那条路），这一趟只管归属和标题。

        if targets:
            stats = await assign_identities(session, targets, extra_titles=titles)
            report.media_updated += stats.moved
            report.season_conflicts += stats.conflicts
            report.media_merged += stats.merged
            report.links_moved += stats.links_moved
            report.links_dropped += stats.links_dropped
            report.parked += stats.parked
            touched |= stats.touched_works
            survivors |= stats.merged_into
        await session.commit()
        if on_progress is not None:
            on_progress(report.media_updated)
    return touched, survivors


async def _recount(
    session: AsyncSession,
    report: RebuildReport,
    works: list[uuid.UUID],
    survivors: list[uuid.UUID],
) -> None:
    """两级重算：先刷季，再刷作品。

    顺序是硬性的 —— `refresh_work_counters` 是从 `media.resource_count` 汇总
    上来的，先刷作品会把旧的季级计数滚上去（见 services/counters.py）。

    季级只刷 `survivors`（吸收了别人关联的存活行），**不刷被动过的 Work 下的
    全部 media**：`refresh_media_counters` 的契约是「顺手物理删除零资源的
    行」，全刷会把这一轮压根没碰过的空壳一起删掉 —— 那是 `maintenance` 的活，
    不是重新分组该有的副作用。

    存活行自己被这一刷删掉是**可以的**：它的关联全迁走之后真的一条资源都没
    有，既搜不出东西也点不开。紧接着的作品级重算会如实把 `season_count` 写小。
    """
    for start in range(0, len(survivors), CHUNK):
        await refresh_media_counters(session, survivors[start : start + CHUNK])
    await session.commit()

    for start in range(0, len(works), CHUNK):
        batch = works[start : start + CHUNK]
        report.works_recounted += await refresh_work_counters(session, batch)
        # 按批提交：中断时这一批要么整批自洽，要么整批没动过。
        await session.commit()


async def rebuild_works(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    key: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> RebuildReport:
    """重算归一键，建 Work，回填归属，合并撞车的季。

    Args:
        dry_run: 只扫描和统计，不建 Work、不改 media。**默认开**。
        key: 只处理这一个 `series_norm_key`，用于单组演练。

    可中断续跑：建 Work 是 get-or-create，回填是幂等的（同样的标题算出同样的
    键，已经归属正确的行一次 UPDATE 都不会发），重算计数本身就是幂等的。
    中断后重跑会把剩下的做完，已经做好的不会被打乱。

    ## 已裁决的键一律跳过

    `title_canon` 里 `status=decided` 的键**不参与重算**。少了这一条，
    重跑 rebuild 会把 LLM 的裁决全部推翻：三本小说被从 `book` 作品里拽回
    规则算出来的垃圾键上（`大主宰:我荒古圣体,当为天帝! 作者:墨之所想` 又变成
    一部独立"作品"），`canon merge` 再跑一遍又搬回去 —— 两个阶段来回拉锯，
    而花过的 token 白花。

    这正是 `title_canon` 存在的理由（见 `models/canon.py`："不会把好不容易
    并好的作品重新拆开"）。规则升级之后想重新分组是正常需求，但重新分组的
    对象只能是**还没裁决过的**那部分；已经判过的要改，改 `title_canon` 那一行
    再跑 `canon merge`。
    """
    report = RebuildReport(dry_run=dry_run)
    aggs = await _scan(session, report, key, await _decided_keys(session))
    if dry_run:
        return report

    work_ids = await _ensure_works(session, aggs, report)
    touched, survivors = await _assign(session, work_ids, report, key, on_progress)
    await _recount(session, report, sorted(touched), sorted(survivors))
    return report
