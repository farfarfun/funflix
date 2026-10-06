"""检测：扫一遍 media，把「按现在的规则算是错的」行落成 `repair_task`。

**只读 + 只写 `repair_task`**，不动一行业务数据。这是和 `apply.py` 分开的
全部理由：检测便宜、可以每轮跑；应用不可逆、要限额。

## 规则没变时必须零写入

每一行都会落到 `plan_repair` 的 `None` 分支，于是一个任务都不建。这是这套
机制能挂在流水线上天天跑的前提 —— 验收线就是「空转一次，`created` 为 0」。

## 幂等靠部分唯一索引，不靠「扫过就不扫了」

同一行每轮都会被重新检出。`uq_repair_task_pending` 保证同一 `(kind, media_id)`
最多只有一个未处理任务；payload 变了就刷新那一行，而不是再插一行。

反过来，**诊断结论变了要撤旧任务**：一行从 `retitle` 变成 `delete`（比如新
补的词表让它洗完成了垃圾），旧的 `retitle` 必须置 `skipped`。否则两个任务
会同时在队列里，先跑 delete 再跑 retitle 就是去改一行已经不存在的 media。
索引是按 `(kind, media_id)` 建的，挡不住这种跨 kind 的并存。

同理，上一轮检出、这一轮已经不需要修的行（parse 顺手覆盖掉了）也要撤 ——
不撤的话队列里会永久积压一批「应用时发现没什么可改」的空任务。

## 分页而不是 `stream()`

`--apply` 时每页提交一次（可中断续跑），而 commit 会把服务端游标关掉。
所以按主键 keyset 翻页，和 `canon/apply.py::_media_ids_for_keys` 同一个做法。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import MediaType
from funflix.models import Media, Work
from funflix.models.base import utcnow
from funflix.models.canon import TitleCanon
from funflix.models.repair import RepairKind, RepairState, RepairTask
from funflix.services.repair.plan import MediaFacts, RepairPlan, plan_key, plan_repair

#: 一页扫多少行 media。沿用 `canon/purge.py` 的 500 —— 远端 PG 一次往返
#: ~100ms，批太小被往返吃掉，批太大则单个事务持锁过久。
CHUNK = 500

#: 留几条样例给人核对，只是展示用。
SAMPLE_LIMIT = 40


@dataclass(slots=True)
class ScanReport:
    scanned: int = 0
    #: 按 kind 统计的检出数（含已经有任务、这轮没新建的）。
    retitle: int = 0
    rehome: int = 0
    delete: int = 0
    #: `rehome` 里目标身份已经被别人占着的数量 —— 应用时会发生多合一。
    expect_merge: int = 0
    created: int = 0
    #: 已有 pending 任务、但 payload 和这轮算出的不一样，刷新了的。
    refreshed: int = 0
    #: 已有 pending 任务且结论一致，什么都没做的。
    unchanged: int = 0
    #: 撤掉的旧任务：诊断结论变了，或者这一行已经不需要修了。
    cancelled: int = 0
    dry_run: bool = True
    samples: list[str] = field(default_factory=list)

    @property
    def planned(self) -> int:
        return self.retitle + self.rehome + self.delete


@dataclass(slots=True)
class _Page:
    """一页的中间结果。"""

    facts: dict[uuid.UUID, MediaFacts] = field(default_factory=dict)
    plans: dict[uuid.UUID, RepairPlan] = field(default_factory=dict)


async def _load_page(
    session: AsyncSession, cursor: uuid.UUID | None, size: int
) -> list[tuple[uuid.UUID, MediaFacts]]:
    """按主键翻一页，顺带把所属 Work 的 `norm_key` 带出来。

    用 outer join —— 迁移 B 之前 `media.work_id` 可空，生产库里 89 万行
    都还没归属。inner join 会把它们整个漏掉，而那些行恰恰是最需要修的。
    """
    query = (
        select(
            Media.id,
            Media.title,
            Media.media_type,
            Media.year,
            Media.season,
            Media.resource_count,
            Work.norm_key,
        )
        .outerjoin(Work, Media.work_id == Work.id)
        .order_by(Media.id)
        .limit(size)
    )
    if cursor is not None:
        query = query.where(Media.id > cursor)
    rows = (await session.execute(query)).all()
    return [
        (
            row[0],
            MediaFacts(
                title=row[1] or "",
                media_type=row[2] or MediaType.UNKNOWN,
                year=row[3],
                season=row[4],
                resource_count=row[5],
                work_norm_key=row[6],
            ),
        )
        for row in rows
    ]


async def _canon_for(session: AsyncSession, keys: set[str]) -> dict[str, TitleCanon]:
    """一次 IN 查完这一页用到的所有裁决行。

    空键不查 —— `title_canon.norm_key` 不会是空串，而带着空键去 IN
    只是白跑一次往返。
    """
    keys.discard("")
    if not keys:
        return {}
    rows = await session.scalars(select(TitleCanon).where(TitleCanon.norm_key.in_(keys)))
    return {row.norm_key: row for row in rows}


async def _mark_expect_merge(session: AsyncSession, page: _Page) -> int:
    """给这一页的 `rehome` 方案标出「目标身份已被占用」。

    这是 dry-run 的预报值，**不是**执行时的判据 —— 真正的撞车合并由
    `assign_identities` 在应用的那一刻自己发现并处理。所以这里只按页统计、
    只求能提前告诉人「这轮大概会发生多少次多合一」，不追求全局精确：
    跨页落到同一身份的两行会各自报 false，而应用时照样会并。
    """
    rehomes = {mid: plan for mid, plan in page.plans.items() if plan.kind == RepairKind.REHOME}
    if not rehomes:
        return 0

    work_keys = {str(plan.payload["work_norm_key"]) for plan in rehomes.values()}
    found = select(Work.id, Work.norm_key).where(Work.norm_key.in_(work_keys))
    work_ids = {norm_key: work_id for work_id, norm_key in (await session.execute(found)).all()}

    identities: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
    for media_id, plan in rehomes.items():
        work_id = work_ids.get(str(plan.payload["work_norm_key"]))
        if work_id is not None:
            identities[media_id] = (work_id, int(plan.payload["season"]))

    occupants: dict[tuple[uuid.UUID, int], uuid.UUID] = {}
    if identities:
        occupants = {
            (work_id, season): media_id
            for media_id, work_id, season in (
                await session.execute(
                    select(Media.id, Media.work_id, Media.season).where(
                        tuple_(Media.work_id, Media.season).in_(list(set(identities.values())))
                    )
                )
            ).all()
        }

    # 同一页里两行指向同一身份也是一次多合一，先到的算占位者。
    claimed: dict[tuple[uuid.UUID, int], uuid.UUID] = {}
    merges = 0
    for media_id, plan in rehomes.items():
        identity = identities.get(media_id)
        sitting = occupants.get(identity) if identity is not None else None
        if sitting is None and identity is not None:
            sitting = claimed.setdefault(identity, media_id)
        if sitting is not None and sitting != media_id:
            plan.payload["expect_merge"] = True
            merges += 1
        else:
            plan.payload["expect_merge"] = False
    return merges


async def _sync_tasks(session: AsyncSession, page: _Page, report: ScanReport) -> None:
    """把这一页的方案和库里已有的 pending 任务对齐。

    四种情形都要处理，漏一种队列就会脏掉（见模块说明）：建新的、刷新
    payload、撤掉换了 kind 的、撤掉已经不需要修的。
    """
    media_ids = list(page.facts)
    existing = (
        await session.execute(
            select(RepairTask).where(
                RepairTask.media_id.in_(media_ids),
                RepairTask.status == RepairState.PENDING,
            )
        )
    ).scalars()
    by_media: dict[uuid.UUID, list[RepairTask]] = {}
    for task in existing:
        by_media.setdefault(task.media_id, []).append(task)

    now = utcnow()
    stale: list[uuid.UUID] = []
    for media_id, tasks in by_media.items():
        plan = page.plans.get(media_id)
        # kind 不等的旧任务一律撤掉：结论变了，或者这一行已经不用修了。
        stale.extend(t.id for t in tasks if plan is None or t.kind != plan.kind)

    if stale:
        await session.execute(
            update(RepairTask)
            .where(RepairTask.id.in_(stale))
            .values(status=RepairState.SKIPPED, applied_at=now)
        )
        report.cancelled += len(stale)

    for media_id, plan in page.plans.items():
        same_kind = next(
            (t for t in by_media.get(media_id, []) if t.kind == plan.kind),
            None,
        )
        if same_kind is None:
            session.add(
                RepairTask(
                    kind=plan.kind,
                    symptom=plan.symptom,
                    media_id=media_id,
                    payload=plan.payload,
                    status=RepairState.PENDING,
                    detected_at=now,
                )
            )
            report.created += 1
        elif same_kind.payload != plan.payload or same_kind.symptom != plan.symptom:
            same_kind.payload = plan.payload
            same_kind.symptom = plan.symptom
            same_kind.detected_at = now
            report.refreshed += 1
        else:
            report.unchanged += 1


def _tally(report: ScanReport, plan: RepairPlan, facts: MediaFacts) -> None:
    if plan.kind == RepairKind.DELETE:
        report.delete += 1
    elif plan.kind == RepairKind.REHOME:
        report.rehome += 1
    else:
        report.retitle += 1
    if len(report.samples) < SAMPLE_LIMIT:
        detail = plan.payload.get("title", facts.title)
        report.samples.append(f"[{plan.kind}/{plan.symptom}] {facts.title!r} → {detail!r}")


async def scan_media(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    key: str | None = None,
    limit: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ScanReport:
    """扫 media，把需要修的行落成 `repair_task`。

    Args:
        dry_run: 只统计，连 `repair_task` 都不写。**默认开**。
        key: 只处理 `series_norm_key(clean_title(title))` 等于它的行。
            **下推不了** —— 这个键是纯函数、库里没有这一列，所以仍然要扫全表，
            只是跳过不匹配的行。用于单组演练。
        limit: 最多**扫**多少行（不是最多建多少任务）。用于分轮磨完全表。
        on_progress: 每页调一次，入参是累计扫过的行数。

    每页提交，可中断续跑。
    """
    report = ScanReport(dry_run=dry_run)
    cursor: uuid.UUID | None = None

    while True:
        size = CHUNK if limit is None else min(CHUNK, limit - report.scanned)
        if size <= 0:
            break
        rows = await _load_page(session, cursor, size)
        if not rows:
            break
        cursor = rows[-1][0]
        report.scanned += len(rows)

        page = _Page()
        for media_id, facts in rows:
            if key is not None and plan_key(facts.title) != key:
                continue
            page.facts[media_id] = facts

        canons = await _canon_for(session, {plan_key(f.title) for f in page.facts.values()})
        for media_id, facts in page.facts.items():
            plan = plan_repair(facts, canons.get(plan_key(facts.title)))
            if plan is not None:
                page.plans[media_id] = plan

        report.expect_merge += await _mark_expect_merge(session, page)
        for media_id, plan in page.plans.items():
            _tally(report, plan, page.facts[media_id])

        if not dry_run:
            await _sync_tasks(session, page, report)
            await session.commit()
        if on_progress is not None:
            on_progress(report.scanned)

    return report
