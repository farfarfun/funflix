"""应用：把 `repair_task` 里的 pending 任务落到 media / work 上。

**不可逆。** 两部剧并成一部之后，没有任何信息能把它们分回去 —— `media_resource`
的关联已经迁走、败者的行已经删掉。所以这一半是默认 dry-run、带限额、带爆炸
半径闸门的，而检测那一半（`scan.py`）可以天天跑。

## 执行顺序是硬性的：delete → retitle → rehome

1. **delete** 先跑。垃圾行和空壳先清掉，直接缩小后面 rehome 的候选集，
   也避免「把一行垃圾并进正常作品」—— 一旦并进去，那条垃圾链接就永久挂在
   真作品下面了。走 `canon/purge.py::delete_media_rows`：删 `media_tag` /
   `media_resource` / `media`，**resource 行保留**（链接是真的，只是归属错了）。
2. **retitle** 再跑。纯 UPDATE，不动身份，最安全。
3. **rehome** 最后。它要 get-or-create 目标 Work，然后交给
   `canon/assign.py::assign_identities` —— 移动、撞车合并、换位停车三件事
   在同一步里完成。**多合一就是它负责的那部分，这里不重新实现。**

## 爆炸半径闸门

一次规则改动不该动到全库的两成。真触发了，更可能是我把规则写错了，
而不是数据真的坏了那么多 —— 而「规则写错 + 自动应用」的组合在生产库上
是不可挽回的。所以超限直接拒绝，要人显式 `--force`。

阈值是按 **media 总行数**算的，不是按队列长度：队列是慢慢攒起来的，
拿它自己当分母的话，攒得越多越容易通过，正好反了。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import MediaType
from funflix.models import Media, Work
from funflix.models.base import utcnow
from funflix.models.media import UNKNOWN_YEAR
from funflix.models.repair import RepairKind, RepairState, RepairTask
from funflix.services.canon.assign import assign_identities
from funflix.services.canon.purge import delete_media_rows
from funflix.services.counters import refresh_media_counters, refresh_work_counters
from funflix.services.repair.plan import plan_key
from funflix.services.text.normalize import norm_key as per_title_key

CHUNK = 500

SAMPLE_LIMIT = 40

#: 删除任务超过 media 总量的这个比例就拒绝执行。
MAX_DELETE_RATIO = 0.05

#: 重挂任务超过 media 总量的这个比例就拒绝执行。
MAX_REHOME_RATIO = 0.20


class BlastRadiusExceeded(RuntimeError):
    """这一轮要动的行数超过了闸门。调用方该把它当成「需要人确认」。"""


@dataclass(slots=True)
class ApplyReport:
    """`delete` / `retitle` / `rehome` 是**领取到的**任务数；
    `deleted` / `retitled` / `rehomed` 是实际改动的行数。两者会不等 ——
    任务指向的 media 可能已经不在了（上一轮作为多合一的败者被删掉）。"""

    delete: int = 0
    retitle: int = 0
    rehome: int = 0
    deleted: int = 0
    links_detached: int = 0
    retitled: int = 0
    rehomed: int = 0
    works_created: int = 0
    works_existing: int = 0
    merged: int = 0
    links_moved: int = 0
    links_dropped: int = 0
    parked: int = 0
    works_recounted: int = 0
    #: 任务指向的 media 已经不存在，或值已经对了，什么都没做。
    skipped: int = 0
    failed: int = 0
    dry_run: bool = True
    samples: list[str] = field(default_factory=list)


async def media_ids_for_key(session: AsyncSession, key: str) -> set[uuid.UUID]:
    """找出作品归一键等于 `key` 的 media 行。

    **只能在 Python 侧匹配** —— `series_norm_key` 是纯函数，库里没有这一列。
    键的算法必须和 `scan.py` 的 `--key` 完全一致（都走 `plan_key`），否则
    `scan --key X` 建出来的任务会被 `apply --key X` 漏掉。

    和 `canon/apply.py::_media_ids_for_keys` 一样按主键翻页、不走 `stream()`：
    调用方后面要边改边提交，游标会被 commit 掉。

    只在单组演练时用 —— 它扫全表，不该出现在流水线的常规路径上。
    """
    found: set[uuid.UUID] = set()
    cursor: uuid.UUID | None = None
    while True:
        query = select(Media.id, Media.title).order_by(Media.id).limit(CHUNK)
        if cursor is not None:
            query = query.where(Media.id > cursor)
        page = (await session.execute(query)).all()
        if not page:
            return found
        cursor = page[-1][0]
        for media_id, title in page:
            if plan_key(title or "") == key:
                found.add(media_id)


async def _claim(
    session: AsyncSession, kind: str, limit: int | None, media_ids: set[uuid.UUID] | None
) -> list[RepairTask]:
    """领一批某一类的 pending 任务。走 `ix_repair_task_queue`。

    按 `detected_at` 排序 —— 先检出的先修，这样「慢慢刷」是公平的，
    不会让某一批行因为主键靠后而永远排不上。
    """
    query = (
        select(RepairTask)
        .where(RepairTask.status == RepairState.PENDING, RepairTask.kind == kind)
        .order_by(RepairTask.detected_at)
    )
    if media_ids is not None:
        query = query.where(RepairTask.media_id.in_(list(media_ids)))
    if limit is not None:
        query = query.limit(limit)
    return list(await session.scalars(query))


async def _check_blast_radius(session: AsyncSession, tasks: dict[str, list[RepairTask]]) -> None:
    total = await session.scalar(select(func.count()).select_from(Media)) or 0
    if total == 0:
        return
    gates = ((RepairKind.DELETE, MAX_DELETE_RATIO), (RepairKind.REHOME, MAX_REHOME_RATIO))
    for kind, ratio in gates:
        count = len(tasks.get(kind, []))
        if count > total * ratio:
            raise BlastRadiusExceeded(
                f"这一轮有 {count} 个 {kind} 任务，超过 media 总量 {total} 的 "
                f"{ratio:.0%}。一次规则改动不该动到这么多行 —— 先核对 "
                f"`funflix repair scan` 的样例，确认规则没写错，再用 --force 放行。"
            )


async def _apply_deletes(
    session: AsyncSession, tasks: list[RepairTask], report: ApplyReport
) -> None:
    """删掉任务指向的 media 行。

    `delete_media_rows` 自己负责计数收尾 —— 行删掉之后就再也查不出它们曾
    属于哪部作品了（见它的 docstring）。这里只把数字并进报告。
    """
    if not tasks:
        return
    by_media = {t.media_id: t for t in tasks}
    alive = set(await session.scalars(select(Media.id).where(Media.id.in_(list(by_media)))))

    now = utcnow()
    for media_id, task in by_media.items():
        if media_id in alive:
            continue
        # 已经不在了（上一轮被别的路径删掉），目的已达成。
        task.status = RepairState.SKIPPED
        task.applied_at = now
        report.skipped += 1

    victims = sorted(alive)
    if victims:
        stats = await delete_media_rows(session, victims)
        report.deleted += stats.deleted
        report.links_detached += stats.links_detached
        report.works_recounted += stats.works_recounted

    for media_id in alive:
        task = by_media[media_id]
        task.status = RepairState.APPLIED
        task.applied_at = now
    await session.commit()


async def _apply_retitles(
    session: AsyncSession, tasks: list[RepairTask], report: ApplyReport
) -> None:
    """纯 UPDATE 刷标题 / 类型 / 年份，不动身份。

    `norm_key` 跟着刷：它是逐标题身份键（不是作品键），标题变了它就该变。
    作品键在 rehome 那条路上，这里不碰。
    """
    now = utcnow()
    for start in range(0, len(tasks), CHUNK):
        batch = tasks[start : start + CHUNK]
        found = select(Media.id, Media.title, Media.media_type, Media.year).where(
            Media.id.in_([t.media_id for t in batch])
        )
        rows: dict[uuid.UUID, tuple[str, MediaType, int]] = {
            row[0]: (row[1], row[2], row[3]) for row in (await session.execute(found)).all()
        }
        for task in batch:
            current = rows.get(task.media_id)
            if current is None:
                task.status = RepairState.SKIPPED
                task.applied_at = now
                report.skipped += 1
                continue
            cur_title, cur_type, cur_year = current
            # payload 是 JSON，取出来的都是 Any —— 缺字段就沿用当前值，
            # 而不是让 `MediaType(None)` 在这里炸掉。
            title = str(task.payload.get("title") or cur_title)
            raw_type = task.payload.get("media_type")
            media_type = MediaType(str(raw_type)) if raw_type else cur_type
            raw_year = task.payload.get("year")
            year = int(raw_year) if isinstance(raw_year, int) else cur_year
            if (title, media_type, year) == current:
                # scan 之后 parse 顺手改对了。
                task.status = RepairState.SKIPPED
                task.applied_at = now
                report.skipped += 1
                continue
            await session.execute(
                update(Media)
                .where(Media.id == task.media_id)
                .values(
                    title=title[:500],
                    norm_key=per_title_key(title)[:500],
                    media_type=media_type,
                    year=year,
                )
            )
            task.status = RepairState.APPLIED
            task.applied_at = now
            report.retitled += 1
        await session.commit()


async def _ensure_work(session: AsyncSession, task: RepairTask, report: ApplyReport) -> uuid.UUID:
    """按 payload 的 `work_norm_key` get-or-create Work。

    已存在的 Work **不覆盖属性**，只在原值为空/unknown 时补 —— 和
    `canon/apply.py::_ensure_work` 同一个契约。它可能是裁决建的、或者人工
    改过，拿一行 media 的推断去覆盖整部作品是不对的。
    """
    work_key = str(task.payload["work_norm_key"])
    media_type = MediaType(task.payload.get("media_type") or MediaType.UNKNOWN)
    year = int(task.payload.get("year", UNKNOWN_YEAR))

    work = await session.scalar(select(Work).where(Work.norm_key == work_key))
    if work is None:
        work = Work(
            norm_key=work_key,
            title=str(task.payload.get("work_title") or work_key)[:500],
            media_type=media_type,
            year=year,
        )
        session.add(work)
        await session.flush()
        report.works_created += 1
        return work.id

    report.works_existing += 1
    if work.media_type is MediaType.UNKNOWN and media_type is not MediaType.UNKNOWN:
        work.media_type = media_type
    if work.year == UNKNOWN_YEAR and year != UNKNOWN_YEAR:
        work.year = year
    return work.id


async def _apply_rehomes(
    session: AsyncSession,
    tasks: list[RepairTask],
    report: ApplyReport,
    on_progress: Callable[[int], None] | None,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """把 media 搬到目标 `(work_id, season)`，撞车就地合并。

    标题**和搬迁在同一步**刷（`assign_identities` 的 `extra_titles`）——
    拆成两步的话，中途崩溃会留下「标题已经改了、身份还没搬」的半成品，
    下一轮 scan 看到的是一个它自己造出来的新症状。

    返回 `(touched_works, survivors)` 给收尾重算用。
    """
    touched: set[uuid.UUID] = set()
    survivors: set[uuid.UUID] = set()
    now = utcnow()

    for start in range(0, len(tasks), CHUNK):
        batch = tasks[start : start + CHUNK]
        alive = set(
            await session.scalars(select(Media.id).where(Media.id.in_([t.media_id for t in batch])))
        )
        targets: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
        titles: dict[uuid.UUID, str] = {}
        for task in batch:
            if task.media_id not in alive:
                task.status = RepairState.SKIPPED
                task.applied_at = now
                report.skipped += 1
                continue
            work_id = await _ensure_work(session, task, report)
            targets[task.media_id] = (work_id, int(task.payload["season"]))  # type: ignore[arg-type]
            title = task.payload.get("title")
            if title:
                titles[task.media_id] = str(title)[:500]

        if targets:
            stats = await assign_identities(session, targets, extra_titles=titles)
            report.rehomed += stats.moved
            report.merged += stats.merged
            report.links_moved += stats.links_moved
            report.links_dropped += stats.links_dropped
            report.parked += stats.parked
            touched |= stats.touched_works
            survivors |= stats.merged_into

        for task in batch:
            if task.status == RepairState.PENDING:
                task.status = RepairState.APPLIED
                task.applied_at = now
        # 按批提交：中断时这一批要么整个应用完，要么完全没动。
        await session.commit()
        if on_progress is not None:
            on_progress(report.rehomed)

    return touched, survivors


async def _recount(
    session: AsyncSession,
    report: ApplyReport,
    works: list[uuid.UUID],
    survivors: list[uuid.UUID],
) -> None:
    """两级重算：先刷季，再刷作品。顺序是硬性的。

    `refresh_work_counters` 是把 `media.resource_count` 加起来，反了作品数
    会停在旧的季级计数上（不报错，但悄悄对不上）。

    季级**只刷 survivors**（合并时吸收了别人关联的存活行）：
    `refresh_media_counters` 会物理删除零资源的行，全刷会连带删掉这一轮
    压根没碰过的空壳。和 `canon/apply.py::_recount` 同一个约束。
    """
    for start in range(0, len(survivors), CHUNK):
        await refresh_media_counters(session, survivors[start : start + CHUNK])
    await session.commit()

    for start in range(0, len(works), CHUNK):
        report.works_recounted += await refresh_work_counters(session, works[start : start + CHUNK])
        await session.commit()


async def apply_repairs(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    key: str | None = None,
    media_ids: set[uuid.UUID] | None = None,
    force: bool = False,
    on_progress: Callable[[int], None] | None = None,
) -> ApplyReport:
    """排空 `repair_task` 里的 pending 任务。

    Args:
        dry_run: 只统计，不写库。**默认开**。
        limit: 每一类最多领多少个任务。节流阀 —— 一轮刷不完下一轮接着刷，
            pending 行天然是断点。
        key: 只处理作品归一键等于它的行，用于单组演练。和 `scan` 的 `--key`
            同一个键空间（见 `media_ids_for_key`）。代价是扫一遍全表。
        media_ids: 只处理这些 media 的任务。给调用方直接点名用；和 `key`
            同时给就取交集。
        force: 跳过爆炸半径闸门。
        on_progress: rehome 阶段每批调一次。

    Raises:
        BlastRadiusExceeded: 要动的行数超过闸门且没给 `force`。
    """
    report = ApplyReport(dry_run=dry_run)
    if key is not None:
        scoped = await media_ids_for_key(session, key)
        media_ids = scoped if media_ids is None else media_ids & scoped
        if not media_ids:
            # 这个键一行都没匹配上。`in_([])` 本身也会返回空，但提前收手
            # 省掉三次必然为空的查询。
            return report
    tasks = {
        kind: await _claim(session, kind, limit, media_ids)
        for kind in (RepairKind.DELETE, RepairKind.RETITLE, RepairKind.REHOME)
    }
    report.delete = len(tasks[RepairKind.DELETE])
    report.retitle = len(tasks[RepairKind.RETITLE])
    report.rehome = len(tasks[RepairKind.REHOME])

    for kind in (RepairKind.DELETE, RepairKind.RETITLE, RepairKind.REHOME):
        for task in tasks[kind][:SAMPLE_LIMIT]:
            report.samples.append(f"[{kind}/{task.symptom}] {task.media_id} {task.payload}")

    if not any(tasks.values()):
        return report

    # 闸门在 dry-run 时**也**检查：dry-run 的作用就是让人提前看到这个拦截。
    if not force:
        await _check_blast_radius(session, tasks)

    if dry_run:
        return report

    await _apply_deletes(session, tasks[RepairKind.DELETE], report)
    await _apply_retitles(session, tasks[RepairKind.RETITLE], report)
    touched, survivors = await _apply_rehomes(
        session, tasks[RepairKind.REHOME], report, on_progress
    )
    await _recount(session, report, sorted(touched), sorted(survivors))
    return report
