"""常驻 worker：周期性地把三条队列各推进一批。见 docs/DESIGN.md §5。

## 关于「启动补偿」

设计文档里 §5.3 要求启动时扫一遍卡住的任务重新投递。这里**没有**单独的
补偿扫描，因为领取条件（见 `claim.py`）本身就同时接受"待处理"和
"处理中但租约已过期"两种行，崩溃遗留的任务在租约到期后会自然回到队列 ——
补偿是领取逻辑的一部分，不需要额外一遍扫描。

启动时只做一次**只读**的体检并打日志，不改任何状态。刻意不去强清租约：
多 worker 部署下，此刻仍未过期的租约可能正被另一个活着的 worker 持有，
清掉它就会造成同一条任务被两个进程同时处理 —— 正是租约要防的事。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.config import Settings, get_settings
from funflix.base.db import session_scope
from funflix.base.enums import CheckStatus, ParseStatus
from funflix.models import RawDocument, Resource, Source, utcnow
from funflix.services.verify.registry import assert_registry_matches_enum
from funflix.services.verify.runner import RateLimiter
from funflix.worker.tasks import (
    BatchReport,
    run_collect_batch,
    run_parse_batch,
    run_verify_batch,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(slots=True)
class CycleReport:
    """一轮扫描的结果。"""

    collect: BatchReport = field(default_factory=BatchReport)
    parse: BatchReport = field(default_factory=BatchReport)
    verify: BatchReport = field(default_factory=BatchReport)

    @property
    def idle(self) -> bool:
        """三条队列都没活干。"""
        return self.collect.idle and self.parse.idle and self.verify.idle

    def summary(self) -> str:
        return (
            f"采集[{self.collect.summary()}] "
            f"解析[{self.parse.summary()}] "
            f"校验[{self.verify.summary()}]"
        )


@dataclass(slots=True)
class StaleSummary:
    """卡在"处理中"且租约已过期的任务数。"""

    documents: int = 0
    resources: int = 0
    sources: int = 0

    @property
    def total(self) -> int:
        return self.documents + self.resources + self.sources


async def stale_summary(session: AsyncSession) -> StaleSummary:
    """体检：有多少任务是上一次崩溃留下的。只读，不改状态。"""
    now = utcnow()

    async def count(model, *conditions) -> int:
        stmt = select(func.count()).select_from(model).where(*conditions)
        return int(await session.scalar(stmt) or 0)

    return StaleSummary(
        documents=await count(
            RawDocument,
            RawDocument.parse_status == ParseStatus.RUNNING,
            or_(RawDocument.lease_until.is_(None), RawDocument.lease_until <= now),
        ),
        resources=await count(
            Resource,
            Resource.check_status == CheckStatus.CHECKING,
            or_(Resource.lease_until.is_(None), Resource.lease_until <= now),
        ),
        sources=await count(
            Source,
            Source.lease_until.isnot(None),
            Source.lease_until <= now,
        ),
    )


@dataclass(slots=True)
class StageCounts:
    """一个阶段当前的数据量：待处理 / 处理中 / 已完成。"""

    pending: int = 0
    running: int = 0
    done: int = 0

    def group(self) -> str:
        return f"{self.pending}/{self.running}/{self.done}"


@dataclass(slots=True)
class ProgressSnapshot:
    """三条队列当前各态数据量的快照，用于运行时心跳打印。"""

    collect: StageCounts = field(default_factory=StageCounts)
    parse: StageCounts = field(default_factory=StageCounts)
    verify: StageCounts = field(default_factory=StageCounts)

    def line(self) -> str:
        return (
            f"采集[{self.collect.group()}] 解析[{self.parse.group()}] 校验[{self.verify.group()}]"
        )


async def progress_snapshot(session: AsyncSession) -> ProgressSnapshot:
    """三条队列当前各状态的数据量。只读，供每隔几秒打一行心跳日志用。

    刻意不复用 `services/stats.py::collect_stats` —— 它还会算 extraction/media/
    resource_orphan 等一堆更重的查询（含一个 NOT EXISTS 子查询），心跳每几秒跑
    一次，只需要 parse_status / check_status 两个分组，不需要拖上那些。
    """
    now = utcnow()

    source_row = (
        await session.execute(
            select(
                func.sum(case((Source.enabled, 1), else_=0)),
                func.sum(
                    case(
                        (
                            Source.enabled
                            & Source.lease_until.isnot(None)
                            & (Source.lease_until > now),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            Source.enabled
                            & or_(Source.lease_until.is_(None), Source.lease_until <= now)
                            & or_(Source.next_fetch_at.is_(None), Source.next_fetch_at <= now),
                            1,
                        ),
                        else_=0,
                    )
                ),
            ).select_from(Source)
        )
    ).one()
    collect_enabled, collect_running, collect_pending = (int(v or 0) for v in source_row)

    async def group(model: Any, column: Any) -> dict[Any, int]:
        rows = await session.execute(
            select(column, func.count()).select_from(model).group_by(column)
        )
        return {key: value for key, value in rows.all()}

    raw_by_status = await group(RawDocument, RawDocument.parse_status)
    parse_pending = raw_by_status.get(ParseStatus.PENDING, 0)
    parse_running = raw_by_status.get(ParseStatus.RUNNING, 0)
    parse_total = sum(raw_by_status.values())

    resource_by_check = await group(Resource, Resource.check_status)
    verify_pending = resource_by_check.get(CheckStatus.UNCHECKED, 0)
    verify_running = resource_by_check.get(CheckStatus.CHECKING, 0)
    verify_total = sum(resource_by_check.values())

    return ProgressSnapshot(
        collect=StageCounts(
            pending=collect_pending,
            running=collect_running,
            done=collect_enabled - collect_pending - collect_running,
        ),
        parse=StageCounts(
            pending=parse_pending,
            running=parse_running,
            done=parse_total - parse_pending - parse_running,
        ),
        verify=StageCounts(
            pending=verify_pending,
            running=verify_running,
            done=verify_total - verify_pending - verify_running,
        ),
    )


async def _run_progress_ticks(
    stop: asyncio.Event,
    interval: int,
    session_factory: SessionFactory,
    on_tick: Callable[[str], None],
) -> None:
    """每隔 `interval` 秒调一次 `on_tick(line)`，直到 `stop` 被置位。

    只读快照，不消费任何任务；查询失败不该拖累调用方，兜在这里自己重试。
    """
    while not stop.is_set():
        try:
            async with session_factory() as session:
                snapshot = await progress_snapshot(session)
            on_tick(snapshot.line())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("进度查询失败，%ss 后重试", interval)

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def _default_on_tick(line: str) -> None:
    logger.info("进度：%s", line)


@asynccontextmanager
async def progress_heartbeat(
    interval: int,
    session_factory: SessionFactory = session_scope,
    *,
    on_tick: Callable[[str], None] | None = None,
) -> AsyncIterator[None]:
    """后台每隔 `interval` 秒汇报一次三条队列当前各态的数据量。

    给一次性命令（`collect` / `parse` / `verify` / `worker --once`）用，
    跟 `Worker.run_forever` 的心跳共享同一份快照逻辑。`interval <= 0` 时
    直接是个空操作，调用方不用自己判断开关。
    """
    if interval <= 0:
        yield
        return

    stop = asyncio.Event()
    task = asyncio.create_task(
        _run_progress_ticks(stop, interval, session_factory, on_tick or _default_on_tick),
        name="funflix-progress-heartbeat",
    )
    try:
        yield
    finally:
        # 只置位 stop、不 cancel()：心跳大多数时候都在 `stop.wait()` 里挂着，
        # set() 就能让它立刻醒来退出。若这一刻正巧卡在一次查询中途，
        # 也让它把这次查询走完再看 stop —— 强行 cancel 会打断 aiosqlite
        # 连接的正常收尾，析构线程在事件循环关掉之后再回调会报一堆噪音。
        stop.set()
        await task


class Worker:
    """把三条队列轮流推进的常驻循环。

    自身不持有会话 —— 每一批任务开一个新会话，跑完就关。长驻进程持有一个
    长事务会把连接一直占住，也让 SQLite 的写锁迟迟不释放。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory or session_scope
        self._limiter = RateLimiter(rate_per_second=self.settings.worker_verify_rate)
        self._lease = timedelta(seconds=self.settings.worker_lease_seconds)

    async def startup_check(self) -> StaleSummary:
        """启动体检。只读，只打日志。"""
        assert_registry_matches_enum()
        async with self._session_factory() as session:
            stale = await stale_summary(session)
        if stale.total:
            logger.warning(
                "发现 %s 条上次未收尾的任务（文档 %s / 资源 %s / 源 %s），"
                "租约已过期，本轮起会被自动重新领取",
                stale.total,
                stale.documents,
                stale.resources,
                stale.sources,
            )
        return stale

    async def run_once(self) -> CycleReport:
        """跑一轮：采集 → 解析 → 校验，各推进到队列清空。

        顺序是有意的 —— 先采集拿到新文本，本轮的解析就能直接吃到它，
        新文本产出的资源又能被本轮的校验捡走，一轮走完整条流水线。

        每个阶段内部会循环分批领取直到队列清空，而不是只推进一批 ——
        某一队列大量积压时会在这一轮里长时间独占，暂时不轮到另外两个阶段，
        这是刻意的取舍：比起"公平但谁都清不完"，用户更想要"跑完就是真的清空了"。
        """
        report = CycleReport()
        cfg = self.settings

        async with self._session_factory() as session:
            report.collect = await run_collect_batch(
                session,
                limit=cfg.worker_collect_batch,
                lease=self._lease,
            )
        async with self._session_factory() as session:
            report.parse = await run_parse_batch(
                session,
                limit=cfg.worker_parse_batch,
                lease=self._lease,
                extractor=cfg.worker_extractor,
                write_batch=cfg.worker_write_batch,
            )
        async with self._session_factory() as session:
            report.verify = await run_verify_batch(
                session,
                limit=cfg.worker_verify_batch,
                lease=self._lease,
                limiter=self._limiter,
                write_batch=cfg.worker_write_batch,
            )
        return report

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """一直跑，直到 `stop` 被置位或任务被取消。"""
        stop = stop or asyncio.Event()
        interval = self.settings.worker_poll_seconds
        await self.startup_check()
        logger.info("worker 启动，轮询间隔 %ss，租约 %s", interval, self._lease)

        async with progress_heartbeat(
            self.settings.worker_progress_seconds,
            self._session_factory,
            on_tick=lambda line: logger.info("worker 进度：%s", line),
        ):
            while not stop.is_set():
                try:
                    report = await self.run_once()
                    if not report.idle:
                        logger.info("worker 一轮完成：%s", report.summary())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 一轮失败不该让整个 worker 退出 —— 下一轮大概率就好了
                    # （数据库重启、网络抖动）。真正的坏任务由重试上限兜住。
                    logger.exception("worker 本轮异常，%ss 后重试", interval)

                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass  # 正常的轮询间隔到点

        logger.info("worker 已停止")


def spawn(settings: Settings | None = None) -> tuple[asyncio.Task[None], asyncio.Event]:
    """把 worker 挂成后台任务，返回 (任务, 停止信号)。

    给 FastAPI 的 lifespan 用：起进程时 spawn，关进程时置位并 await。
    """
    stop = asyncio.Event()
    worker = Worker(settings)
    task = asyncio.create_task(worker.run_forever(stop), name="funflix-worker")
    return task, stop
