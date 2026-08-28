"""`funflix parse` 的并发执行引擎：用 funworker 把生产/处理/消费三段解耦。

`runner.py` 里的 `parse_batch`/`persist_extracted` 全程单线程跑；这里把同一套
落库逻辑套进 funworker 的 Producer → WorkerPool(Processor) → Consumer 流水线，
用多线程并发跑 `extractor.extract()`（通常是耗时的网络/LLM 调用），从而缩短
`funflix parse --limit N` 的总耗时。

funworker 是纯线程 + `queue.Queue` 模型，完全不感知 asyncio，而 funflix 全链路
是 SQLAlchemy async。`AsyncEngine`/`AsyncSession` 绑定在创建它们的事件循环上，
不能跨线程复用——所以只有生产者线程和消费者线程碰数据库，各自在 `on_start()`
里用 `db.create_engine()`（不是进程级单例 `get_engine()`）现造一个专属引擎，
配一个专属的、线程内持久化的事件循环，线程结束时 `dispose()` 掉。处理单元
（Processor）线程池完全不碰数据库，只负责 `extract()`/`rehydrate()`。

缓存命中判断（跳过 `extract()`）依赖数据库查询，留在生产者侧：生产者翻页取
出一页 `RawDocument` 后顺手做一次批量缓存查询，把 `cached_output` 随文档内容
一起塞进发给处理单元的条目。处理单元收到条目后，命中缓存走 `rehydrate()`
（同步、无 IO），没命中才调用 `extract()`。

`extract()` 抛异常时处理单元自己捕获，转成带 `error` 字段的结果传给消费者——
不能让它变成 `WorkerPool` 默认的重试/丢弃语义，那样就没机会把失败写回
`doc.parse_attempts`/`next_parse_at`。消费者复用 `persist_extracted` 的退避
逻辑统一处理"处理单元报告的失败"和"落库时自己抛出的失败"两种来源。

进度用 `on_progress(total_enqueued, total_committed)` 轮询喂给调用方：分母是
"入队列的总数"（`producer.stats()["produced"]`，随生产者翻页动态增长），
分子是"消费者真正提交到数据库的文档数"（见下面 `_ParseConsumer`）。

`_ParseConsumer` 继承 `funworker.BaseBatchConsumer`，攒批/计时轮询逻辑交给
基类，只实现 `consume_batch`。分子用的是 `BaseBatchConsumer.stats()`
自带的 `consumed` 计数——它只在 `consume_batch` 跑完之后才按整批累加，
语义上就是"落库成功的文档数"；跟处理单元线程池的 `processed` 计数、跟普通
`BaseConsumer.consumed`（逐条累加，只反映"内存里处理完了"）不是一回事。
落库（消费者单线程、每条约等于一次 SAVEPOINT+若干 INSERT/UPDATE 往返）比
抽取慢得多时，用后两者会让进度条冲到 100% 后卡住一大截——用户看到的是
"瞬间跑完"，但数据库其实还在慢慢追。进度条要如实反映"写进去了多少"，不是
"内存里处理到哪了"，宁可看起来爬得慢，也不能显示假的"已完成"。

但"提交数"不能同时兼任轮询循环的收尾信号：消费者攒够 `write_batch` 条或等到
`flush_interval` 才落库一次，样本量小、或落库比抽取慢时，提交数可能在生产者
已经退出、两条队列也都空了之后依然追不上入队总数——循环会一直等一个永远
不会自然发生的"提交数追上总数"，`pipeline.stop()`（连带它触发的收尾 flush）
就永远不会被调用，直接卡死。所以收尾判断改用 `_pipeline_pending`：只看两条
队列的 qsize 是否都归零，跟提交数、跟处理单元的 `processed` 都无关，保证一定
能收敛，再把"是否已经彻底跑空"和"进度条显示到哪了"分成两件事。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from funworker import BaseBatchConsumer, BaseProcessor, BaseProducer, Pipeline
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from funflix.base.config import Settings, get_settings
from funflix.base.db import create_engine
from funflix.base.enums import ParseStatus
from funflix.models import RawDocument, utcnow
from funflix.services.extract.base import Extractor
from funflix.services.extract.registry import default_extractor_for, get_extractor
from funflix.services.extract.runner import (
    ParseReport,
    _load_cached_batch,
    keyset_after,
    persist_extracted,
)


def _pending_conditions(now: Any) -> tuple[Any, ...]:
    return (
        RawDocument.parse_status == ParseStatus.PENDING,
        or_(RawDocument.next_parse_at.is_(None), RawDocument.next_parse_at <= now),
    )


async def count_pending(session: AsyncSession, *, limit: int | None) -> int:
    """待解析文档总数，供调用方渲染进度条，不影响流水线本身。"""
    now = utcnow()
    total = int(
        await session.scalar(
            select(func.count()).select_from(RawDocument).where(*_pending_conditions(now))
        )
        or 0
    )
    return min(total, limit) if limit is not None else total


class _ParseProducer(BaseProducer):
    """翻页读取待解析文档，附上缓存命中情况后逐条吐给处理单元线程池。"""

    def __init__(
        self,
        output_queue: Any,
        *,
        settings: Settings,
        extractor_override: str | None,
        limit: int | None,
        batch_size: int,
        force: bool,
        name: str | None = None,
    ) -> None:
        super().__init__(output_queue, name=name)
        self.settings = settings
        self.extractor_override = extractor_override
        self.limit = limit
        self.batch_size = batch_size
        self.force = force

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._extractor_cache: dict[str, Extractor] = {}
        self._buffer: list[dict[str, Any]] = []
        self._last_ts: Any = None
        self._last_id = 0
        self._remaining_limit = self.limit
        self._exhausted = False

    def on_stop(self) -> None:
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def _extractor_for(self, kind: str) -> Extractor:
        if kind not in self._extractor_cache:
            self._extractor_cache[kind] = get_extractor(kind)
        return self._extractor_cache[kind]

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
            docs = list(
                await session.scalars(
                    select(RawDocument)
                    .where(
                        *_pending_conditions(now),
                        keyset_after(
                            RawDocument.last_parsed_at, RawDocument.id, self._last_ts, self._last_id
                        ),
                    )
                    .order_by(RawDocument.last_parsed_at.nulls_first(), RawDocument.id)
                    .limit(fetch_n)
                )
            )
            if not docs:
                self._exhausted = True
                return

            self._last_ts = docs[-1].last_parsed_at
            self._last_id = docs[-1].id
            if self._remaining_limit is not None:
                self._remaining_limit -= len(docs)

            groups: dict[str, list[RawDocument]] = {}
            for doc in docs:
                kind = self.extractor_override or default_extractor_for(doc.source_type)
                groups.setdefault(kind, []).append(doc)

            for kind, kind_docs in groups.items():
                impl = self._extractor_for(kind)
                cached_by_doc = (
                    {}
                    if self.force
                    else await _load_cached_batch(
                        session, [d.id for d in kind_docs], impl.name, impl.version
                    )
                )
                for doc in kind_docs:
                    cached = cached_by_doc.get(doc.id)
                    self._buffer.append(
                        {
                            "doc_id": doc.id,
                            "content": doc.content,
                            "extractor_kind": kind,
                            "cached_output": cached.output if cached is not None else None,
                        }
                    )


class _ParseProcessor(BaseProcessor):
    """并发跑 `extract()`（或缓存命中时 `rehydrate()`），不碰数据库。"""

    def __init__(self, *, extractor_override: str | None) -> None:
        self.extractor_override = extractor_override

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._extractor_cache: dict[str, Extractor] = {}

    def on_stop(self) -> None:
        self._aio_loop.close()

    def _extractor_for(self, kind: str) -> Extractor:
        if kind not in self._extractor_cache:
            self._extractor_cache[kind] = get_extractor(kind)
        return self._extractor_cache[kind]

    def process(self, item: dict[str, Any]) -> Any:
        extractor = self._extractor_for(item["extractor_kind"])
        try:
            if item["cached_output"] is not None:
                outcome = extractor.rehydrate(item["cached_output"], item["content"])
            else:
                outcome = self._aio_loop.run_until_complete(extractor.extract(item["content"]))
        except Exception as exc:
            return {
                "doc_id": item["doc_id"],
                "extractor_kind": item["extractor_kind"],
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "doc_id": item["doc_id"],
            "extractor_kind": item["extractor_kind"],
            "outcome": outcome,
            "from_cache": item["cached_output"] is not None,
        }


class _ParseConsumer(BaseBatchConsumer):
    """攒够 `write_batch` 条，或距上次落库过了 `flush_interval` 秒（或流水线收尾时），批量落库一次。

    继承 `funworker.BaseBatchConsumer`：攒批按条数触发，缓冲区未满时也保证
    最多等 `flush_interval` 秒就强制落库一次，这个轮询由基类负责，不依赖
    "下一条数据到达才检查"，这里只实现 `consume_batch`。
    """

    def __init__(
        self,
        input_queue: Any,
        *,
        settings: Settings,
        write_batch: int,
        flush_interval: float = 10.0,
        name: str | None = None,
    ) -> None:
        super().__init__(
            input_queue, batch_size=write_batch, batch_timeout=flush_interval, name=name
        )
        self.settings = settings
        self.reports: list[ParseReport] = []

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._extractor_cache: dict[str, Extractor] = {}

    def on_stop(self) -> None:
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def _extractor_for(self, kind: str) -> Extractor:
        if kind not in self._extractor_cache:
            self._extractor_cache[kind] = get_extractor(kind)
        return self._extractor_cache[kind]

    def consume_batch(self, items: list[dict[str, Any]]) -> None:
        self._aio_loop.run_until_complete(self._flush_batch(items))

    async def _flush_batch(self, items: list[dict[str, Any]]) -> None:
        doc_ids = [it["doc_id"] for it in items]
        async with self._sessionmaker() as session:
            try:
                rows = list(
                    await session.scalars(select(RawDocument).where(RawDocument.id.in_(doc_ids)))
                )
                docs_by_id = {d.id: d for d in rows}

                outcomes: dict[int, Any] = {}
                cached_doc_ids: set[int] = set()
                extraction_errors: dict[int, str] = {}
                by_extractor: dict[str, list[RawDocument]] = {}

                for it in items:
                    doc = docs_by_id.get(it["doc_id"])
                    if doc is None:
                        # 落库前文档被删了（人工干预），跳过，不阻塞整批。
                        continue
                    by_extractor.setdefault(it["extractor_kind"], []).append(doc)
                    if "error" in it:
                        extraction_errors[doc.id] = it["error"]
                        continue
                    outcomes[doc.id] = it["outcome"]
                    if it["from_cache"]:
                        cached_doc_ids.add(doc.id)

                reports: list[ParseReport] = []
                for kind, kind_docs in by_extractor.items():
                    extractor = self._extractor_for(kind)
                    reports.extend(
                        await persist_extracted(
                            session,
                            kind_docs,
                            outcomes,
                            cached_doc_ids,
                            extractor,
                            extraction_errors=extraction_errors,
                        )
                    )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        self.reports.extend(reports)


def _pipeline_counts(pipeline: Pipeline) -> tuple[int, int]:
    """(总入队数, 总提交数) —— 提交数按消费者真正 `session.commit()` 成功的文档数算。

    不用处理单元线程池的 `processed` 计数：只反映"内存里处理完了"，不反映
    "落库成功了"，详见模块 docstring。`BaseBatchConsumer.stats()["consumed"]`
    才是"落库成功"的口径，跟普通 `BaseConsumer.consumed`（逐条累加）语义不同。
    """
    total = pipeline.producer.stats()["produced"]
    done = pipeline.consumer.stats()["consumed"]
    return total, done


def _pipeline_pending(pipeline: Pipeline) -> int:
    """两条队列里当前还没被取走的积压条数，用来判断"是否已经彻底跑空"。

    不能拿"提交数追上入队数"（`_pipeline_counts` 的返回值）当收尾信号：消费者
    是攒够 `write_batch` 条或等到 `flush_interval` 才落库一次，样本量小、或
    落库比抽取慢时，提交数可能在两条队列都空、生产者也退出之后依然追不上
    入队数——循环会永远等不到"完成"，`pipeline.stop()` 就永远不会被调用。
    这里只看队列 qsize：队列空了、生产者也不在跑了，就说明再没有新数据会
    进来，可以放心收尾——`pipeline.stop()` 里 `stage.stop(drain=True)` 会等
    飞行中的条目跑完，消费者 `on_stop()` 会把内部缓冲区里的残留条目做最后
    一次落库，理由同 `services/collect/concurrent_runner.py::_pipeline_pending`。
    """
    pending = pipeline.producer.stats()["output_qsize"]
    if pipeline.consumer is not None:
        pending += pipeline.consumer.stats()["input_qsize"]
    return pending


def run_parse_pipeline(
    *,
    extractor_name: str | None = None,
    limit: int | None = None,
    batch_size: int = 500,
    write_batch: int = 20,
    flush_interval: float = 10.0,
    concurrency: int = 4,
    force: bool = False,
    settings: Settings | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[ParseReport]:
    """跑一次完整的生产者/处理单元/消费者流水线，返回消费者攒的全部报告。

    同步阻塞函数——funworker 本身是阻塞式设计，不需要外层 `asyncio.run` 包装。
    生产/消费两端各自持有专属的 `AsyncEngine` + 事件循环（见模块 docstring），
    处理单元线程池并发数由 `concurrency` 控制。消费者最多攒 `write_batch` 条
    或每 `flush_interval` 秒批量落库一次，取先满足的那个条件。

    `on_progress(total_enqueued, total_done)` 每 0.5 秒轮询一次，即便生产者
    已经翻完页（规划通常比 `extract()` 快得多），只要队列里还有积压就继续
    轮询——不然进度条会在处理单元/消费者还在忙的时候看起来像卡死了。
    """
    settings = settings or get_settings()

    def processor_factory() -> _ParseProcessor:
        return _ParseProcessor(extractor_override=extractor_name)

    pipeline = Pipeline.build(
        _ParseProducer,
        processor_factory,
        _ParseConsumer,
        num_workers=max(1, concurrency),
        producer_kwargs={
            "settings": settings,
            "extractor_override": extractor_name,
            "limit": limit,
            "batch_size": batch_size,
            "force": force,
        },
        consumer_kwargs={
            "settings": settings,
            "write_batch": write_batch,
            "flush_interval": flush_interval,
        },
    )
    pipeline.start()
    try:
        while True:
            if pipeline.producer.is_alive():
                pipeline.producer.join(timeout=0.5)
            else:
                time.sleep(0.5)
            if on_progress is not None:
                on_progress(*_pipeline_counts(pipeline))
            if not pipeline.producer.is_alive() and _pipeline_pending(pipeline) == 0:
                break
    finally:
        pipeline.stop()
    if on_progress is not None:
        on_progress(*_pipeline_counts(pipeline))

    consumer = pipeline.consumer
    assert isinstance(consumer, _ParseConsumer)
    return consumer.reports
