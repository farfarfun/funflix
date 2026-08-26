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
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, or_, select
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
        """跑一轮：采集 → 解析 → 校验，各推进一批。

        顺序是有意的 —— 先采集拿到新文本，本轮的解析就能直接吃到它，
        新文本产出的资源又能被本轮的校验捡走，一轮走完整条流水线。
        """
        report = CycleReport()
        cfg = self.settings

        async with self._session_factory() as session:
            report.collect = await run_collect_batch(
                session, limit=cfg.worker_collect_batch, lease=self._lease
            )
        async with self._session_factory() as session:
            report.parse = await run_parse_batch(
                session,
                limit=cfg.worker_parse_batch,
                lease=self._lease,
                extractor=cfg.worker_extractor,
            )
        async with self._session_factory() as session:
            report.verify = await run_verify_batch(
                session,
                limit=cfg.worker_verify_batch,
                lease=self._lease,
                limiter=self._limiter,
            )
        return report

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """一直跑，直到 `stop` 被置位或任务被取消。"""
        stop = stop or asyncio.Event()
        interval = self.settings.worker_poll_seconds
        await self.startup_check()
        logger.info("worker 启动，轮询间隔 %ss，租约 %s", interval, self._lease)

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
