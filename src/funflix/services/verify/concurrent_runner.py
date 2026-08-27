"""`funflix verify` 的并发执行引擎：用 funworker 把生产/处理/消费三段解耦。

结构照抄 `services/extract/concurrent_runner.py`：生产者线程翻页读 `Resource`，
处理单元线程池并发跑 `probe.check()`（网络 IO），消费者线程批量落库/提交。
`services/verify/runner.py` 里的 `check_resource` 全程单线程跑，继续留给
`worker/tasks.py::run_verify_batch`（常驻 worker 模式，走租约领取，是完全
独立的调用路径）；这里只服务 `funflix verify` 这条一次性批处理 CLI 命令。

处理单元线程只负责 `probe.check()`，不碰数据库——探针本身是无状态的（见
`registry.get_probe`），线程私有缓存一份即可。限流器要跨线程共享同一个
`BlockingRateLimiter` 实例（`asyncio.Lock` 版的 `RateLimiter` 绑在各自线程的
事件循环上，不能跨线程用），这样"每个网盘每秒最多几次请求"才是全局生效。

落库逻辑（写 `LinkCheck`、推进 `resource.check_status`/`next_check_at`、
刷新作品的 `valid_resource_count`）留给消费者线程，复用
`services/verify/runner.py::persist_check_outcome`。

进度用 `on_progress(total_enqueued, total_done)` 按生产者/消费者的累计计数
轮询喂给调用方（做法照抄 `services/collect/concurrent_runner.py`）：分子是
"处理过的总数"，分母是"入队列的总数"——入队快照会随生产者继续翻页动态
增长，不会像"启动前查一次待校验数就固定住"那样跟实际进度对不上。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from funworker import BaseConsumer, BaseProcessor, BaseProducer, Pipeline
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from funflix.base.config import Settings, get_settings
from funflix.base.db import create_engine
from funflix.base.enums import CHECKABLE_PROVIDERS, CheckStatus, Provider
from funflix.models import Resource, utcnow
from funflix.services.extract.runner import keyset_after
from funflix.services.verify.base import CheckOutcome, LinkProbe, LinkRef
from funflix.services.verify.registry import get_probe
from funflix.services.verify.runner import BlockingRateLimiter, VerifyReport, persist_check_outcome


def _due_conditions(now: Any, *, recheck_all: bool) -> tuple[Any, ...]:
    conditions: list[Any] = [Resource.provider.in_(CHECKABLE_PROVIDERS)]
    if not recheck_all:
        conditions.append(
            or_(
                Resource.next_check_at.is_(None) & (Resource.check_status == CheckStatus.UNCHECKED),
                Resource.next_check_at <= now,
            )
        )
    return tuple(conditions)


async def count_due(session: AsyncSession, *, recheck_all: bool, limit: int | None) -> int:
    """待校验资源总数，供调用方渲染进度条，不影响流水线本身。"""
    now = utcnow()
    conditions = _due_conditions(now, recheck_all=recheck_all)
    total = int(
        await session.scalar(select(func.count()).select_from(Resource).where(*conditions)) or 0
    )
    return min(total, limit) if limit is not None else total


class _VerifyProducer(BaseProducer):
    """按 `(last_checked_at, id)` 复合游标翻页读取待校验资源，逐条吐给处理单元线程池。"""

    def __init__(
        self,
        output_queue: Any,
        *,
        settings: Settings,
        limit: int | None,
        batch_size: int,
        recheck_all: bool,
        name: str | None = None,
    ) -> None:
        super().__init__(output_queue, name=name)
        self.settings = settings
        self.limit = limit
        self.batch_size = batch_size
        self.recheck_all = recheck_all

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._buffer: list[dict[str, Any]] = []
        self._last_ts: Any = None
        self._last_id = 0
        self._remaining_limit = self.limit
        self._exhausted = False

    def on_stop(self) -> None:
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def produce(self) -> Any:
        if not self._buffer and not self._exhausted:
            self._aio_loop.run_until_complete(self._fetch_page())
        if not self._buffer:
            raise StopIteration
        return self._buffer.pop(0)

    async def _fetch_page(self) -> None:
        fetch_n = self.batch_size
        if self._remaining_limit is not None:
            fetch_n = min(fetch_n, self._remaining_limit)
        if fetch_n <= 0:
            self._exhausted = True
            return

        now = utcnow()
        async with self._sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(Resource)
                    .where(
                        *_due_conditions(now, recheck_all=self.recheck_all),
                        keyset_after(
                            Resource.last_checked_at, Resource.id, self._last_ts, self._last_id
                        ),
                    )
                    .order_by(Resource.last_checked_at.nulls_first(), Resource.id)
                    .limit(fetch_n)
                )
            )
            if not rows:
                self._exhausted = True
                return

            self._last_ts = rows[-1].last_checked_at
            self._last_id = rows[-1].id
            if self._remaining_limit is not None:
                self._remaining_limit -= len(rows)

            for row in rows:
                self._buffer.append(
                    {
                        "resource_id": row.id,
                        "provider": row.provider,
                        "share_id": row.share_id,
                        "url": row.url,
                        "passcode": row.passcode,
                    }
                )


class _VerifyProcessor(BaseProcessor):
    """并发跑 `probe.check()`，不碰数据库。"""

    def __init__(self, *, rate_limiter: BlockingRateLimiter) -> None:
        self.rate_limiter = rate_limiter

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._probe_cache: dict[Provider, LinkProbe | None] = {}

    def on_stop(self) -> None:
        self._aio_loop.close()

    def _probe_for(self, provider: Provider) -> LinkProbe | None:
        if provider not in self._probe_cache:
            self._probe_cache[provider] = get_probe(provider)
        return self._probe_cache[provider]

    def process(self, item: dict[str, Any]) -> Any:
        provider = item["provider"]
        probe = self._probe_for(provider)
        if probe is None:
            # `_due_conditions` 已经把查询限定在 CHECKABLE_PROVIDERS 里，
            # 按注册表的一致性约束这里不应该发生——留个兜底而不是断言，
            # 避免注册表将来漂移时把整条流水线炸掉。
            outcome = CheckOutcome(
                status=CheckStatus.UNSUPPORTED, detail=f"没有 {provider.value} 的探针"
            )
            return {
                "resource_id": item["resource_id"],
                "outcome": outcome,
                "probe_name": "unsupported",
            }

        self.rate_limiter.acquire(provider)
        ref = LinkRef(
            provider=provider, share_id=item["share_id"], url=item["url"], passcode=item["passcode"]
        )
        try:
            outcome = self._aio_loop.run_until_complete(probe.check(ref))
        except Exception as exc:
            # 探针骨架本身已经兜底了自己的异常，这层纯防御性——防止未来新探针
            # 实现漏掉兜底时，一次异常把整条流水线拖垮。
            outcome = CheckOutcome(status=CheckStatus.ERROR, detail=f"{type(exc).__name__}: {exc}")
        return {"resource_id": item["resource_id"], "outcome": outcome, "probe_name": probe.name}


class _VerifyConsumer(BaseConsumer):
    """攒够 `write_batch` 条（或流水线收尾时）批量落库一次。"""

    def __init__(
        self,
        input_queue: Any,
        *,
        settings: Settings,
        write_batch: int,
        name: str | None = None,
    ) -> None:
        super().__init__(input_queue, name=name)
        self.settings = settings
        self.write_batch = write_batch
        self.reports: list[VerifyReport] = []

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._buffer: list[dict[str, Any]] = []

    def on_stop(self) -> None:
        if self._buffer:
            self._aio_loop.run_until_complete(self._flush())
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def consume(self, item: dict[str, Any]) -> None:
        self._buffer.append(item)
        if len(self._buffer) >= self.write_batch:
            self._aio_loop.run_until_complete(self._flush())

    async def _flush(self) -> None:
        items, self._buffer = self._buffer, []
        if not items:
            return

        resource_ids = [it["resource_id"] for it in items]
        reports: list[VerifyReport] = []
        async with self._sessionmaker() as session:
            try:
                rows = list(
                    await session.scalars(select(Resource).where(Resource.id.in_(resource_ids)))
                )
                rows_by_id = {r.id: r for r in rows}

                for it in items:
                    resource = rows_by_id.get(it["resource_id"])
                    if resource is None:
                        # 落库前资源被删了（人工干预），跳过，不阻塞整批。
                        continue
                    reports.append(
                        await persist_check_outcome(
                            session, resource, it["outcome"], probe_name=it["probe_name"]
                        )
                    )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        self.reports.extend(reports)


def _pipeline_counts(pipeline: Pipeline) -> tuple[int, int]:
    """(总入队数, 总处理数) —— 处理数按消费者读完队列、处理完（成功或失败）算，

    只算成功会在有失败时永远追不上入队数。
    """
    total = pipeline.producer.stats()["produced"]
    consumer = pipeline.consumer
    assert consumer is not None
    consumer_stats = consumer.stats()
    done = consumer_stats["consumed"] + consumer_stats["failed"]
    return total, done


def _pipeline_finished(*, producer_alive: bool, total: int, done: int) -> bool:
    """理由同 `services/extract/concurrent_runner.py::_pipeline_finished`。"""
    return not producer_alive and done >= total


def run_verify_pipeline(
    *,
    limit: int | None = None,
    batch_size: int = 500,
    write_batch: int = 100,
    concurrency: int = 8,
    rate: float = 5.0,
    recheck_all: bool = False,
    settings: Settings | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[VerifyReport]:
    """跑一次完整的生产者/处理单元/消费者流水线，返回消费者攒的全部报告。

    同步阻塞函数——funworker 本身是阻塞式设计，不需要外层 `asyncio.run` 包装。
    生产/消费两端各自持有专属的 `AsyncEngine` + 事件循环（见模块 docstring），
    处理单元线程池并发数由 `concurrency` 控制，全部线程共享同一个
    `BlockingRateLimiter`。

    `on_progress(total_enqueued, total_done)` 每 0.5 秒轮询一次，理由同
    `services/extract/concurrent_runner.py::run_parse_pipeline`。
    """
    settings = settings or get_settings()
    rate_limiter = BlockingRateLimiter(rate_per_second=rate)

    def processor_factory() -> _VerifyProcessor:
        return _VerifyProcessor(rate_limiter=rate_limiter)

    pipeline = Pipeline.build(
        _VerifyProducer,
        processor_factory,
        _VerifyConsumer,
        num_workers=max(1, concurrency),
        producer_kwargs={
            "settings": settings,
            "limit": limit,
            "batch_size": batch_size,
            "recheck_all": recheck_all,
        },
        consumer_kwargs={
            "settings": settings,
            "write_batch": write_batch,
        },
    )
    pipeline.start()
    try:
        while True:
            if pipeline.producer.is_alive():
                pipeline.producer.join(timeout=0.5)
            else:
                time.sleep(0.5)
            total, done = _pipeline_counts(pipeline)
            if on_progress is not None:
                on_progress(total, done)
            if _pipeline_finished(
                producer_alive=pipeline.producer.is_alive(), total=total, done=done
            ):
                break
    finally:
        pipeline.stop()
    if on_progress is not None:
        on_progress(*_pipeline_counts(pipeline))

    consumer = pipeline.consumer
    assert isinstance(consumer, _VerifyConsumer)
    return consumer.reports
