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
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from funworker import BaseConsumer, BaseProcessor, BaseProducer, Pipeline
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


class _ParseConsumer(BaseConsumer):
    """攒够 `write_batch` 条（或流水线收尾时）批量落库一次。"""

    def __init__(
        self,
        input_queue: Any,
        *,
        settings: Settings,
        write_batch: int,
        on_progress: Callable[[int], None] | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(input_queue, name=name)
        self.settings = settings
        self.write_batch = write_batch
        self.on_progress = on_progress
        self.reports: list[ParseReport] = []

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._extractor_cache: dict[str, Extractor] = {}
        self._buffer: list[dict[str, Any]] = []

    def on_stop(self) -> None:
        if self._buffer:
            self._aio_loop.run_until_complete(self._flush())
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def _extractor_for(self, kind: str) -> Extractor:
        if kind not in self._extractor_cache:
            self._extractor_cache[kind] = get_extractor(kind)
        return self._extractor_cache[kind]

    def consume(self, item: dict[str, Any]) -> None:
        self._buffer.append(item)
        if len(self._buffer) >= self.write_batch:
            self._aio_loop.run_until_complete(self._flush())

    async def _flush(self) -> None:
        items, self._buffer = self._buffer, []
        if not items:
            return

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
        if self.on_progress is not None:
            self.on_progress(len(items))


def run_parse_pipeline(
    *,
    extractor_name: str | None = None,
    limit: int | None = None,
    batch_size: int = 500,
    write_batch: int = 100,
    concurrency: int = 4,
    force: bool = False,
    settings: Settings | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> list[ParseReport]:
    """跑一次完整的生产者/处理单元/消费者流水线，返回消费者攒的全部报告。

    同步阻塞函数——funworker 本身是阻塞式设计，不需要外层 `asyncio.run` 包装。
    生产/消费两端各自持有专属的 `AsyncEngine` + 事件循环（见模块 docstring），
    处理单元线程池并发数由 `concurrency` 控制。
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
            "on_progress": on_progress,
        },
    )
    pipeline.run(install_signal_handlers=False)

    consumer = pipeline.consumer
    assert isinstance(consumer, _ParseConsumer)
    return consumer.reports
