"""阶段 4：把 `title_canon` 里的裁决落到 media / work 上。

模块叫 `apply` 而不是 `merge` —— `merge.py` 是「把几行 media 并成一行」那个
共享原子操作，阶段 2 和这一阶段都在用。这里是**阶段**，负责决定哪些行该并。

做三件事，顺序要紧：

1. `is_junk` 的键 → 走阶段 1 的删除路径。先删，省得它们继续参与后面的归并。
2. 其余键 → 把 media 重挂到裁决出的 Work 上（按需新建 Work），并按裁决覆盖
   `season`；落到同一个 `(work_id, season)` 的行在搬迁的**同一步**里就并掉
   （`canon/assign.py`，不能先搬完再回头并，中途就撞唯一键了）。
3. 两级重算计数：先刷季、再刷作品。

## `season=None` 不是「第 0 季」

`title_canon.season` 回答的是「**这个键本身**锁定了哪一季」，不是「这部作品有
几季」。一个 `series_norm_key` 通常横跨多季（键里的季号已经被 `strip_season`
摘掉了），所以：

- `season` 是具体数字 → 这个键锁定了那一季，**覆盖**规则逐行判出的季号。
  典型是 `大主宰2` 这种规则没认出来的季号写法。
- `season is None` → 这个键没锁定季，**不动**规则逐行判出的季号。
  典型是 `大主宰` 这个通名键，它底下的行各自是第 1 季、第 2 季、未定。

把 None 当 0 处理会把一部剧的所有季压成一行，正是这次改造要消掉的毛病。

## 空出来的 Work 不删

重挂之后原来的 Work 可能一行 media 都不剩。这里**不删**它，只把计数刷成 0
（`refresh_work_counters` 的契约就是这样，和会顺手删空 media 的
`refresh_media_counters` 不同）。理由是审计：`title_canon` 记着"这个键被并到
哪去了"，空 Work 留着就还能顺着 `work.norm_key` 对上账。真要清理，
`maintenance` 那边单独加一条命令更合适。
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
from funflix.models.media import UNKNOWN_YEAR
from funflix.services.canon.assign import assign_identities
from funflix.services.canon.purge import delete_media_rows
from funflix.services.counters import refresh_media_counters, refresh_work_counters
from funflix.services.text.normalize import series_norm_key

CHUNK = 500
SAMPLE_LIMIT = 30


@dataclass(slots=True)
class ApplyReport:
    decisions: int = 0
    junk_keys: int = 0
    junk_media_deleted: int = 0
    links_detached: int = 0
    works_created: int = 0
    works_existing: int = 0
    media_rehomed: int = 0
    seasons_overridden: int = 0
    season_conflicts: int = 0
    media_merged: int = 0
    links_moved: int = 0
    links_dropped: int = 0
    #: 为了解开换位冲突而临时挪动的次数，见 `canon/assign.py`。
    parked: int = 0
    works_recounted: int = 0
    #: 上一轮已经落完、这一轮没新活，整键跳过的数量。见 `_already_settled`。
    settled: int = 0
    #: 真有活要干、但被这一轮的 `limit` 挡在外面的键数，下一轮接着做。
    deferred: int = 0
    #: 不表达作品身份、因此 merge 无活可干的裁决数。见循环里的说明。
    typed_only: int = 0
    dry_run: bool = True
    samples: list[str] = field(default_factory=list)


async def _load_decisions(session: AsyncSession, key: str | None) -> list[TitleCanon]:
    """捞出全部已裁决的行。

    **每一轮都得重新捞全部** —— 裁决不能在应用后标记成"已应用"。parse 还在
    源源不断产出新 media，新行的标题算出来的键可能早就裁过了，那它得按那条老
    裁决归位。标记掉就等于让后来的 media 永远归不了位。

    代价是这张表单调增长、每轮的候选集越来越大，由 `_already_settled` 把"上一
    轮已经落完、这一轮没新活"的键筛掉来兜，见那个函数的说明。

    `key` 按 `title_canon.norm_key` 过滤 —— 这一列**在库里**（不像
    `series_norm_key` 是纯函数），所以能下推到 SQL。
    """
    query = select(TitleCanon).where(TitleCanon.status == CanonState.DECIDED)
    if key is not None:
        query = query.where(TitleCanon.norm_key == key)
    # 定序是为了 `limit` 能切得稳：先裁的先应用，一轮没排上的下一轮还排在前面，
    # 不会出现某些键永远轮不到。`decided_at` 老数据可能是 NULL，排在最前面。
    query = query.order_by(TitleCanon.decided_at.nulls_first(), TitleCanon.norm_key)
    return list(await session.scalars(query))


@dataclass(slots=True)
class _MediaRow:
    """一行 media 的身份快照，`_already_settled` 靠它判断这一轮有没有活要干。"""

    media_id: uuid.UUID
    work_id: uuid.UUID | None
    season: int


async def _media_rows_for_keys(session: AsyncSession, keys: set[str]) -> dict[str, list[_MediaRow]]:
    """按 `series_norm_key` 找出对应的 media 行，连当下的归属一起带出来。

    **只能在 Python 侧匹配** —— `series_norm_key` 是纯函数，库里没有这一列。
    所以这里扫一遍 media，算键再归组。按主键翻页，不走 `stream()`：调用方后面
    要边并边提交，游标会被 commit 掉。

    顺手多取 `work_id` / `season` 是为了 `_already_settled` —— 这一趟全表扫反正
    躲不掉（生产库 170 万行，实测约 1 分钟），把判断"要不要动"所需的列一起捞
    回来，就省掉了后面每个键各自一次查询。
    """
    found: dict[str, list[_MediaRow]] = {k: [] for k in keys}
    cursor: uuid.UUID | None = None
    while True:
        query = (
            select(Media.id, Media.title, Media.work_id, Media.season)
            .order_by(Media.id)
            .limit(CHUNK)
        )
        if cursor is not None:
            query = query.where(Media.id > cursor)
        page = (await session.execute(query)).all()
        if not page:
            break
        cursor = page[-1][0]
        for media_id, title, work_id, season in page:
            bucket = found.get(series_norm_key(title or ""))
            if bucket is not None:
                bucket.append(_MediaRow(media_id=media_id, work_id=work_id, season=season))
    return found


def _work_key_of(decision: TitleCanon) -> str:
    """裁决指向的作品归一键。跟 `_ensure_work` 用的是同一个算法，别让它们分叉。"""
    return decision.work_norm_key or series_norm_key(decision.work_title or "")


async def _existing_works(session: AsyncSession, work_keys: set[str]) -> dict[str, Work]:
    """批量捞出这些归一键已经存在的 Work。分片查，不堆一个几千项的 IN。"""
    keys = sorted(work_keys)
    found: dict[str, Work] = {}
    for start in range(0, len(keys), CHUNK):
        rows = await session.scalars(
            select(Work).where(Work.norm_key.in_(keys[start : start + CHUNK]))
        )
        found.update({w.norm_key: w for w in rows})
    return found


def _already_settled(decision: TitleCanon, rows: list[_MediaRow], work: Work | None) -> bool:
    """这个键上一轮已经落完了、这一轮没有新活 —— 可以整键跳过。

    为什么非要这个判断：`_load_decisions` 每轮都得把全部 `decided` 重新过一遍
    （理由见那边），可那张表是单调涨的 —— resolve 每轮再加几百条。每个键一次
    `assign_identities` 加一次 commit，5,417 条就要两个小时，正好把 Action 的
    job 预算吃光：run 37726067206 的 canon 就是这么 `cancelled` 的，resolve 裁
    出来的 492 条裁决一条都没落库。

    筛掉之后成本从"裁决条数"变成"真有活要干的键数"，而后者只随新进的 media 走。

    判定靠 `uq_media_season (work_id, season)`：已经挂在目标作品下的那些行，季号
    必然互不相同，所以不存在"都就位了但还得并一下"的情况，只看归属就够。
    """
    if work is None:
        # 目标 Work 还没建出来，这一键必须走 `_ensure_work`
        return False
    if work.media_type is MediaType.UNKNOWN and decision.media_type is not MediaType.UNKNOWN:
        # Work 身上还缺的信息要靠这条裁决补齐，见 `_ensure_work`
        return False
    if work.year == UNKNOWN_YEAR and decision.year != UNKNOWN_YEAR:
        return False
    if any(row.work_id != work.id for row in rows):
        return False
    # 裁决锁了季号：整组该并成一行落在那一季。没锁就只换作品，季号各自不动，
    # 归属对上就算落完了。
    if decision.season is not None:
        return all(row.season == decision.season for row in rows)
    return True


async def _ensure_work(
    session: AsyncSession, decision: TitleCanon, report: ApplyReport
) -> uuid.UUID:
    """按裁决的 `work_norm_key` get-or-create Work。

    已存在的 Work **不覆盖属性** —— 它可能是阶段 2 建的、已经被其它裁决
    补过信息，或者人工改过。只在原值为空/unknown 时补。
    """
    work_key = decision.work_norm_key or series_norm_key(decision.work_title or "")
    work = await session.scalar(select(Work).where(Work.norm_key == work_key))
    if work is None:
        work = Work(
            norm_key=work_key,
            title=(decision.work_title or work_key)[:500],
            media_type=decision.media_type,
            year=decision.year,
        )
        session.add(work)
        await session.flush()
        report.works_created += 1
        return work.id

    report.works_existing += 1
    if work.media_type is MediaType.UNKNOWN and decision.media_type is not MediaType.UNKNOWN:
        work.media_type = decision.media_type
    if work.year == UNKNOWN_YEAR and decision.year != UNKNOWN_YEAR:
        work.year = decision.year
    return work.id


async def _targets_for(
    session: AsyncSession,
    decision: TitleCanon,
    work_id: uuid.UUID,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[uuid.UUID, int]]:
    """算出这组 media 各自该落到哪个 `(work_id, season)`。

    裁决锁定了季号就整组落到那一季（于是整组会被并成一行）；没锁定就**保留
    每一行当下的季号**，只换作品 —— 这正是 `season is None` 的语义，
    见模块开头那段说明。保留季号得先把当下的季号读回来，不能省这一次查询。
    """
    if decision.season is not None:
        return dict.fromkeys(ids, (work_id, decision.season))

    seasons: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
    for start in range(0, len(ids), CHUNK):
        rows = (
            await session.execute(
                select(Media.id, Media.season).where(Media.id.in_(ids[start : start + CHUNK]))
            )
        ).all()
        seasons.update({mid: (work_id, season) for mid, season in rows})
    return seasons


async def _recount(
    session: AsyncSession,
    report: ApplyReport,
    works: list[uuid.UUID],
    survivors: list[uuid.UUID],
) -> None:
    """两级重算：先刷季，再刷作品。

    顺序是硬性的 —— `refresh_work_counters` 是把 `media.resource_count` 加
    起来，反了作品数会停在旧的季级计数上（不报错，但悄悄对不上）。

    季级只刷 `survivors`（合并时吸收了别人关联的存活行）。重挂本身不改一行
    的资源数，所以别的行没必要刷 —— 而且**不能**顺手全刷：
    `refresh_media_counters` 会物理删除零资源的行，那会连带删掉这一轮压根
    没碰过的空壳。存活行自己被删掉是可以的（关联全迁走了，真的空了），
    紧接着的作品级重算会如实把 `season_count` 写小。
    """
    for start in range(0, len(survivors), CHUNK):
        await refresh_media_counters(session, survivors[start : start + CHUNK])
    await session.commit()

    for start in range(0, len(works), CHUNK):
        batch = works[start : start + CHUNK]
        report.works_recounted += await refresh_work_counters(session, batch)
        await session.commit()


async def apply_canon_decisions(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    key: str | None = None,
    limit: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ApplyReport:
    """把 `title_canon` 里 `status=decided` 的裁决落到库上。

    Args:
        dry_run: 只统计，不写库。**默认开**。
        key: 只应用 `title_canon.norm_key` 等于它的那一条。
        limit: 这一轮最多处理多少个**真有活要干**的键；None 表示不限。
            额度花在筛完之后的那批上（见 `_already_settled`），不是花在
            `decided` 的总条数上 —— 否则额度会被几千个"已经落完"的键吃掉，
            一轮下来一件事都没干。
        on_progress: 每处理完一个键调一次，入参是累计重挂的 media 行数。

    按键提交，可中断续跑。
    """
    report = ApplyReport(dry_run=dry_run)
    decisions = await _load_decisions(session, key)
    report.decisions = len(decisions)
    if not decisions:
        return report

    by_key = {d.norm_key: d for d in decisions}
    members = await _media_rows_for_keys(session, set(by_key))
    works = await _existing_works(session, {k for d in decisions if (k := _work_key_of(d))})

    junk_victims: list[uuid.UUID] = []
    live: list[tuple[TitleCanon, list[uuid.UUID]]] = []
    for canon_key, decision in by_key.items():
        rows = members.get(canon_key) or []
        if decision.is_junk:
            report.junk_keys += 1
            junk_victims.extend(row.media_id for row in rows)
        elif not _work_key_of(decision):
            # 这条裁决没说作品是谁，merge 就没有归并可做。两种来源：
            #
            # - 分类裁决（`resolver.py` 的阶段 2）：按设计只补 `media_type`，
            #   `work_title` / `work_norm_key` 都是 NULL。它的结论经
            #   `lookup.py` 的 `canon.work_title or title` 走 parse / repair
            #   那条路落到 media 上，不经过这里。
            # - 坏数据：人工改库改出来的空标题行。
            #
            # **必须显式跳过**，不能让它往下走：`_ensure_work` 对空键会
            # get-or-create 一个 `norm_key=''` 的 Work，而所有这样的裁决都会
            # 落到同一个它身上 —— 一个把几万行 media 吸进去的黑洞，且
            # `media.work_id` 改完就再也分不开了。
            report.typed_only += 1
        elif rows:
            if _already_settled(decision, rows, works.get(_work_key_of(decision))):
                report.settled += 1
                continue
            live.append((decision, [row.media_id for row in rows]))

    if limit is not None and len(live) > limit:
        report.deferred = len(live) - limit
        live = live[:limit]

    for decision, ids in live[:SAMPLE_LIMIT]:
        season = "不动" if decision.season is None else str(decision.season)
        report.samples.append(
            f"{decision.norm_key} → {decision.work_title}（季 {season}，{len(ids)} 行）"
        )

    if dry_run:
        report.media_rehomed = sum(len(ids) for _d, ids in live)
        report.seasons_overridden = sum(len(ids) for d, ids in live if d.season is not None)
        return report

    if junk_victims:
        # `delete_media_rows` 自己把丢了季的 Work 计数刷好了（它必须自己刷 ——
        # 行删掉之后就再也查不出那些行曾属于哪部作品）。这里只是把数字并进报告。
        stats = await delete_media_rows(session, junk_victims)
        report.junk_media_deleted = stats.deleted
        report.links_detached = stats.links_detached
        report.works_recounted += stats.works_recounted

    touched_works: set[uuid.UUID] = set()
    survivors: set[uuid.UUID] = set()
    for decision, ids in live:
        work_id = await _ensure_work(session, decision, report)
        targets = await _targets_for(session, decision, work_id, ids)
        if decision.season is not None:
            report.seasons_overridden += len(ids)

        # 搬迁和合并是**同一步** —— 不能先一条 UPDATE 把整组挪到
        # `(work_id, season)` 再回头并重复：裁决锁定季号时整组的目标身份是
        # 同一个，第二行就撞 `uq_media_season`（详见 canon/assign.py）。
        stats = await assign_identities(session, targets)
        report.media_rehomed += stats.moved
        report.season_conflicts += stats.conflicts
        report.media_merged += stats.merged
        report.links_moved += stats.links_moved
        report.links_dropped += stats.links_dropped
        report.parked += stats.parked
        # `touched_works` 含搬离前的旧归属：不刷的话它们会停在"还挂着这些行"
        # 的旧计数上。
        touched_works |= stats.touched_works
        survivors |= stats.merged_into
        # 按键提交：中断时这个键要么整个应用完，要么完全没动。
        await session.commit()
        if on_progress is not None:
            on_progress(report.media_rehomed)

    await _recount(session, report, sorted(touched_works), sorted(survivors))
    return report
