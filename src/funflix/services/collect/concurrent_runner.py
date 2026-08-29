"""`funflix collect` 的并发执行引擎：用 funworker 把生产/处理/消费三段解耦。

`runner.py` 里的 `collect_source`/`_run_backfill` 继续做常驻 worker
（`worker/tasks.py`）和 API 单源触发（`api/v1/sources.py`）的顺序参照实现，
原样保留、不动——它按 `CommitBatcher` 的节奏边跑边提交，这个增量落盘的
安全网是它的核心设计，硬套进多源共享一条队列的并发模型会丢掉这个保证，
不值得为了复用几十行代码去冒险改动一段测试覆盖很重的生产路径。

这里另起一条给 `funflix collect` CLI 命令用的并发流水线，思路是把"翻页"
从"抓内容"里解耦出来：

- Telegram 追新：内部逻辑跟内容强相关（水位落在哪取决于这一页实际翻到了
  什么），当成一个不透明的整源任务扔给处理单元，原样复用
  `TelegramChannelCollector.fetch()`。
- Telegram 补历史：消息 ID 单调递增、每页固定条数，可以不等实际抓到内容
  就靠整数运算把后续每一页的 `before` 游标提前算出来（见
  `telegram.plan_backfill_pages`），拆成多个可并发抓取的"翻一页"任务，
  跟其它源的任务共享同一条队列。规划好之后立刻把新的低水位写库、提交，
  不等这些页真的被处理完——这带来的代价是进程中途异常时，已规划但还没
  被消费到的页会被跳过；这是可接受的：内容用 content_hash 做数据库唯一
  约束兜底去重（见 `services/ingest.py`），偶发丢页不会重复入库，每周的
  水位重置（`Source.reset_watermark`）也会把整个源重新刷一遍，丢的内容
  下次全量重扫时能补回来。
- 腾讯文档（表格/文本）：翻页语义跟内容强绑定（表格要先读一页才知道总
  行数和有几个 sheet；文本文档干脆没有分页），没法提前规划，也当成一个
  不透明的整源任务，内部翻页逻辑原样在处理单元线程里跑完
  （`collector.fetch()` + `collector.backfill()`）。

跟 `extract/concurrent_runner.py` 一样的线程/事件循环桥接方式：只有生产者
和消费者线程碰数据库，各自在 `on_start()` 里用 `db.create_engine()`
（不是进程级单例）现造专属引擎 + 专属的、线程内持久化的事件循环
（命名 `self._aio_loop`，不能叫 `self._loop`——那是 `funworker.BaseWorker`
保留的方法名）。处理单元线程池完全不碰数据库，只负责 HTTP 抓取 + 解析。

`funworker.Pipeline` 的消费者永远只有一个线程，不受 `--concurrency` 影响；
`_CollectConsumer` 跟 `_ParseConsumer`/`_VerifyConsumer` 一样继承
`funworker.BaseBatchConsumer`，攒够 `write_batch` 条或每 `flush_interval`
秒批量落库一次，而不是逐条开 session、逐条提交——8 个处理单元线程并发
抓取带来的吞吐提升，如果消费者还是一条一提交（一条约 2~3 次远程往返，见
`services/ingest.py` 的批量去重注释），会被这个单线程的落库速度重新拖回
串行水平，加再多并发线程也无济于事。攒批/计时逻辑本身不用自己写：
`BaseBatchConsumer` 内部用 `Queue.get(timeout=...)` 主动轮询，`flush_interval`
超时是真正靠时间轮询触发的，不依赖"下一条数据到达才检查"（早期手搓版本
的隐患）。

进度不再按"处理了百分之几"算——一个源可能被拆成几十个并发页任务，
"整体百分比"这个概念本身就不成立了。改用生产者/消费者的累计计数喂给
`on_progress(total_enqueued, total_done)`：总数随生产者规划出更多任务动态
增长，已完成数取消费者真正提交成功的条目数。`BaseBatchConsumer` 的
`consumed` 计数只在 `consume_batch` 跑完之后才按整批累加，语义上就是
"落库成功的条目数"，不再需要额外维护一个 `_committed` 字段——这跟自带的
`BaseConsumer.consumed`（逐条累加，只反映"进了消费者的缓冲区"）不是一回
事，用错了会让进度条冲到 100% 后卡住一大截，交给调用方（CLI）驱动一个
真正会跑动的 `tqdm` 进度条，而不是只有文字后缀的空转指示器。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from funworker import BaseBatchConsumer, BaseProcessor, BaseProducer, Pipeline
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from funflix.base.backoff import MAX_BACKOFF, backoff
from funflix.base.config import Settings, get_settings
from funflix.base.db import create_engine
from funflix.models import Source, utcnow
from funflix.services.collect.base import CollectedMessage, Collector, FetchResult
from funflix.services.collect.registry import get_collector_class
from funflix.services.collect.runner import (
    _BACKFILL_TIME_BUDGET,
    CollectReport,
    _advance_cursor,
    _ingest_messages,
)
from funflix.services.collect.telegram import (
    _MAX_BACKFILL_PAGES_PER_RUN,
    TelegramChannelCollector,
    fetch_page_html,
    parse_channel_page,
    plan_backfill_pages,
)

logger = logging.getLogger(__name__)

#: 每次生产者扫到一个 Telegram 源时最多规划这么多页历史——跟 `backfill()`
#: 原有的单次页数上限保持一致，只是含义从"单次调用实际翻了几页"变成
#: "这一轮规划了几页任务"，量级选取的理由相同。
_TELEGRAM_PAGES_PER_VISIT = _MAX_BACKFILL_PAGES_PER_RUN

#: 传给处理单元线程的 `Source` 快照只需要这些字段——采集器的 `fetch()`/
#: `backfill()`、水位推进逻辑用得到的全在这里，任何一个都不会碰数据库。
#: `consecutive_failures` 必须在列——`_run_opaque_source` 失败分支会在快照上
#: 做 `+= 1`，漏了它默认是 `None`（模型的 `default=0` 只在真正 flush 时才生效，
#: 一个从没挂过 session 的裸 `Source()` 拿不到），失败一次就直接 TypeError 崩掉。
_SOURCE_SNAPSHOT_FIELDS = (
    "id",
    "source_type",
    "identifier",
    "title",
    "extra",
    "cursor_message_id",
    "cursor_published_at",
    "backfill_cursor_id",
    "backfill_done",
    "max_pages_per_fetch",
    "fetch_interval_seconds",
    "consecutive_failures",
)

#: 整源任务成功时，处理单元在快照上推进的字段要原样搬回真正挂在 session
#: 上的那一行。`last_fetched_at` 不在这里——它在生产者规划阶段就已经
#: 落库，处理单元不需要也不会碰它。
_OPAQUE_COPY_FIELDS = (
    "title",
    "extra",
    "cursor_message_id",
    "cursor_published_at",
    "backfill_cursor_id",
    "backfill_done",
    "consecutive_failures",
    "last_error",
    "last_success_at",
    "next_fetch_at",
)


def _detached_copy(source: Source) -> Source:
    """造一个不挂在任何 session 上的快照，安全地跨线程传给处理单元。

    处理单元线程会在这个快照上原地推进水位字段（水位推进逻辑跟
    `runner.collect_source` 保持一致，见 `_run_opaque_source`），这些字段
    值之后由消费者原样搬回真正挂在 session 上的那一行——快照本身绝不会被
    flush/commit。
    """
    copy = Source()
    for name in _SOURCE_SNAPSHOT_FIELDS:
        setattr(copy, name, getattr(source, name))
    return copy


def _build_collector(source_type: Any, client: httpx.AsyncClient) -> Collector:
    cls = get_collector_class(source_type)
    assert cls is not None
    return cls(client=client)  # type: ignore[call-arg]


@dataclass(slots=True)
class _ErrorJob:
    """规划阶段就能确定失败的源（如没有对应类型的采集器），直接带着报告走完流水线。"""

    report: CollectReport
    identifier: str


@dataclass(slots=True)
class _OpaqueJob:
    """整源任务：追新（可选连带补历史）在处理单元线程里一次跑完，不拆分。"""

    source: Source
    do_backfill: bool


@dataclass(slots=True)
class _OpaqueResult:
    source: Source
    fetch_messages: list[CollectedMessage]
    backfill_messages: list[CollectedMessage]
    report: CollectReport


@dataclass(slots=True)
class _TelegramPageJob:
    """Telegram 补历史的一页——`before` 游标已经在生产者阶段算好。"""

    source_id: uuid.UUID
    identifier: str
    before: int


@dataclass(slots=True)
class _PageResult:
    source_id: uuid.UUID
    messages: list[CollectedMessage]


@dataclass(slots=True)
class BackfillPageTotals:
    """并发翻页规划出的 Telegram 补历史任务的汇总统计。

    这类任务分散在多个源、跟其它整源任务交织在一条队列里并发处理，没有
    一个自然的时间点能拼出"某个源的补历史完整报告"，所以只按全局汇总，
    不按源拆分——跟整源任务（`CollectReport`，仍按源展示）刻意不同。
    """

    pages: int = 0
    created: int = 0
    duplicated: int = 0
    skipped: int = 0


@dataclass(slots=True)
class CollectPipelineResult:
    reports: list[tuple[str, CollectReport]] = field(default_factory=list)
    backfill_pages: BackfillPageTotals = field(default_factory=BackfillPageTotals)


def _apply_fetch_result(source: Source, result: FetchResult) -> None:
    """搬 `runner.collect_source` 追新那段的水位/状态推进逻辑（不含落库）。

    与 `runner.py` 里的版本刻意保持逻辑一致而不是抽出来共用——那边的版本
    跟 `_ingest_messages`/`CommitBatcher` 交织在一起，抽取的风险和收益不
    成正比，`collect_source` 是重度测试过的生产路径，不该为了这条新增
    流水线去动它。`_advance_cursor` 本身已经是纯函数，直接导入复用。
    """
    if result.title and not source.title:
        source.title = result.title

    if result.state:
        merged = {**source.extra, **result.state}
        source.extra = {k: v for k, v in merged.items() if v is not None}

    newest = max((m.numeric_id for m in result.messages if m.numeric_id is not None), default=None)
    _advance_cursor(source, result, newest)

    if source.backfill_cursor_id is None:
        oldest = min(
            (m.numeric_id for m in result.messages if m.numeric_id is not None), default=None
        )
        if oldest is not None:
            source.backfill_cursor_id = str(oldest)
        elif source.cursor_message_id:
            source.backfill_cursor_id = source.cursor_message_id

    if result.backfill_pending and source.backfill_done:
        source.backfill_done = False


def _apply_backfill_result(source: Source, result: FetchResult) -> None:
    """搬 `runner._run_backfill` 的水位/状态推进逻辑（不含落库）。"""
    if result.state:
        source.extra = {**source.extra, **result.state}
    if result.backfill_cursor is not None:
        source.backfill_cursor_id = result.backfill_cursor
    if result.backfill_done:
        source.backfill_done = True


async def _run_opaque_source(
    source: Source, collector: Collector, *, now: datetime, do_backfill: bool
) -> tuple[list[CollectedMessage], list[CollectedMessage], CollectReport]:
    """处理单元线程里跑一个"整源"任务：抓取 + （可选）补历史，全程不碰数据库。

    直接复用 `collector.fetch()`/`collector.backfill()`——两者本身就不碰
    DB。落库、按实际入库条数推进 `total_collected`/`total_backfilled`
    留给消费者做（那些数字依赖真实的 ingest 结果，处理单元这里算不出来）。
    """
    report = CollectReport(source_id=source.id, cursor_before=source.cursor_message_id)
    fetch_messages: list[CollectedMessage] = []
    backfill_messages: list[CollectedMessage] = []

    try:
        result = await collector.fetch(source)
    except Exception as exc:
        source.consecutive_failures += 1
        source.last_error = f"{type(exc).__name__}: {exc}"
        source.next_fetch_at = now + backoff(source.consecutive_failures)
        report.error = source.last_error
        logger.warning("采集失败 source=%s: %s", source.identifier, source.last_error)
        return fetch_messages, backfill_messages, report

    report.fetched = len(result.messages)
    report.pages_fetched = result.pages_fetched
    report.truncated = result.truncated
    fetch_messages = result.messages
    _apply_fetch_result(source, result)

    if do_backfill:
        deadline = now + _BACKFILL_TIME_BUDGET
        while not source.backfill_done and utcnow() < deadline:
            try:
                b_result = await collector.backfill(source)
            except Exception as exc:
                logger.warning("补历史失败 source=%s: %s", source.identifier, exc)
                break
            backfill_messages.extend(b_result.messages)
            report.backfilled += len(b_result.messages)
            report.pages_fetched += b_result.pages_fetched
            _apply_backfill_result(source, b_result)
            if b_result.pages_fetched == 0:
                break

    source.consecutive_failures = 0
    source.last_error = None
    source.last_success_at = now
    pending_more = report.truncated or not source.backfill_done
    source.next_fetch_at = now + (
        timedelta(seconds=5) if pending_more else timedelta(seconds=source.fetch_interval_seconds)
    )
    report.cursor_after = source.cursor_message_id
    report.backfill_cursor = source.backfill_cursor_id
    report.backfill_done = source.backfill_done
    return fetch_messages, backfill_messages, report


class _CollectProducer(BaseProducer):
    """翻页读取启用的源，规划任务后逐条吐给处理单元线程池。

    Telegram 源的补历史页游标在这里靠整数运算规划好、立刻提交（乐观推进，
    见模块 docstring）；其余情况都规划成一个不透明的整源任务。
    """

    def __init__(
        self,
        output_queue: Any,
        *,
        settings: Settings,
        source_id: uuid.UUID | None,
        batch_size: int,
        limit: int | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(output_queue, name=name)
        self.settings = settings
        self.source_id = source_id
        self.batch_size = batch_size
        self.limit = limit

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._buffer: list[Any] = []
        # 全零 UUID 当"比任何真实 UUIDv7 都小"的哨兵，理由同
        # `services/extract/concurrent_runner.py::_ParseProducer.on_start`。
        self._last_id: uuid.UUID = uuid.UUID(int=0)
        self._exhausted = False
        self._single_done = False

    def on_stop(self) -> None:
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def produce(self) -> Any:
        if not self._buffer and not self._exhausted:
            if self.limit is not None and self._produced >= self.limit:
                #: 已经规划够 `limit` 个任务了——这一次 collect 到此为止，不再扫
                #: 描剩下的源。它们没被跳过，下次 collect 重新按 id 升序扫描时
                #: 会从头再来（`_last_id` 只在本实例内存里，不跨进程持久化）。
                self._exhausted = True
            else:
                self._aio_loop.run_until_complete(self._fetch_page())
        if not self._buffer:
            raise StopIteration
        return self._buffer.pop(0)

    async def _fetch_page(self) -> None:
        async with self._sessionmaker() as session:
            if self.source_id is not None:
                if self._single_done:
                    self._exhausted = True
                    return
                source = await session.get(Source, self.source_id)
                sources = [source] if source is not None else []
                if source is None:
                    self._buffer.append(
                        _ErrorJob(
                            report=CollectReport(
                                source_id=self.source_id, error=f"源 #{self.source_id} 不存在"
                            ),
                            identifier=f"#{self.source_id}",
                        )
                    )
                self._single_done = True
            else:
                sources = list(
                    await session.scalars(
                        select(Source)
                        .where(Source.enabled, Source.id > self._last_id)
                        .order_by(Source.id)
                        .limit(self.batch_size)
                    )
                )
                if not sources:
                    self._exhausted = True
                    return
                self._last_id = sources[-1].id

            now = utcnow()
            for source in sources:
                if self.limit is not None and self._produced + len(self._buffer) >= self.limit:
                    #: 本轮已经攒够 limit——没扫到的源留给下次 collect，别再往
                    #: buffer 里塞更多任务了。
                    break
                self._plan_source(source, now)

            await session.commit()

    def _plan_source(self, source: Source, now: datetime) -> None:
        source.last_fetched_at = now
        collector_cls = get_collector_class(source.source_type)
        if collector_cls is None:
            report = CollectReport(source_id=source.id, cursor_before=source.cursor_message_id)
            report.error = f"没有 {source.source_type.value} 类型的采集器"
            source.last_error = report.error
            source.next_fetch_at = now + MAX_BACKOFF
            self._buffer.append(_ErrorJob(report=report, identifier=source.identifier))
            return

        if collector_cls is TelegramChannelCollector:
            max_pages = _TELEGRAM_PAGES_PER_VISIT
            if self.limit is not None:
                #: 单个源的补历史一次最多能规划 1000 页，budget 快见底时得掐
                #: 短它，不然游标会乐观提交到远超本轮实际入队的位置。
                remaining = self.limit - self._produced - len(self._buffer)
                max_pages = max(min(max_pages, remaining), 0)
            pages, new_cursor, done = plan_backfill_pages(
                source.backfill_cursor_id, max_pages=max_pages
            )
            if pages:
                source.backfill_cursor_id = new_cursor
                if done:
                    source.backfill_done = True
            self._buffer.append(_OpaqueJob(source=_detached_copy(source), do_backfill=False))
            for before in pages:
                self._buffer.append(
                    _TelegramPageJob(
                        source_id=source.id, identifier=source.identifier, before=before
                    )
                )
        else:
            self._buffer.append(_OpaqueJob(source=_detached_copy(source), do_backfill=True))


class _CollectProcessor(BaseProcessor):
    """并发跑 HTTP 抓取 + 解析，不碰数据库。"""

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    def on_stop(self) -> None:
        self._aio_loop.run_until_complete(self._client.aclose())
        self._aio_loop.close()

    def process(self, item: Any) -> Any:
        return self._aio_loop.run_until_complete(self._process(item))

    async def _process(self, item: Any) -> Any:
        if isinstance(item, _ErrorJob):
            return item
        if isinstance(item, _TelegramPageJob):
            return await self._process_page(item)
        if isinstance(item, _OpaqueJob):
            return await self._process_opaque(item)
        raise TypeError(f"未知的采集任务类型: {type(item)!r}")

    async def _process_page(self, item: _TelegramPageJob) -> _PageResult:
        try:
            html = await fetch_page_html(self._client, item.identifier, item.before)
            messages, _title = parse_channel_page(html, item.identifier)
        except Exception as exc:
            logger.warning(
                "补历史翻页失败 source_id=%s before=%s: %s", item.source_id, item.before, exc
            )
            messages = []
        # 只保留比 before 更旧的消息——parse_channel_page 只按 ID 排序，
        # 不保证服务端严格只返回 < before 的部分。
        fresh = [m for m in messages if m.numeric_id is not None and m.numeric_id < item.before]
        return _PageResult(source_id=item.source_id, messages=fresh)

    async def _process_opaque(self, item: _OpaqueJob) -> _OpaqueResult:
        collector = _build_collector(item.source.source_type, self._client)
        fetch_messages, backfill_messages, report = await _run_opaque_source(
            item.source, collector, now=utcnow(), do_backfill=item.do_backfill
        )
        return _OpaqueResult(
            source=item.source,
            fetch_messages=fetch_messages,
            backfill_messages=backfill_messages,
            report=report,
        )


class _CollectConsumer(BaseBatchConsumer):
    """攒够 `write_batch` 条，或距上次落库过了 `flush_interval` 秒（或流水线收尾时），批量落库一次。

    继承 `funworker.BaseBatchConsumer`，攒批/计时轮询逻辑交给基类，这里只
    实现 `consume_batch`。整源任务顺带把水位字段写回真正的 Source 行。同一
    批里多个 Telegram 翻页结果如果落在同一个源上，合并成一次
    `_ingest_messages` 调用——Telegram 补历史一个源一次运行能拆出上千个翻页
    任务，逐条查询/提交的往返耗时会远超实际抓取耗时（见模块 docstring、
    `services/ingest.py` 的批量去重注释），攒批处理才能让 `--concurrency`
    条抓取线程的并发收益真正体现在总耗时上，理由同
    `extract/concurrent_runner.py::_ParseConsumer`。
    """

    def __init__(
        self,
        input_queue: Any,
        *,
        settings: Settings,
        write_batch: int = 100,
        flush_interval: float = 10.0,
        name: str | None = None,
    ) -> None:
        super().__init__(
            input_queue, batch_size=write_batch, batch_timeout=flush_interval, name=name
        )
        self.settings = settings
        self.reports: list[tuple[str, CollectReport]] = []
        self.page_totals = BackfillPageTotals()

    def on_start(self) -> None:
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )

    def on_stop(self) -> None:
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def consume_batch(self, items: list[Any]) -> None:
        self._aio_loop.run_until_complete(self._flush_batch(items))

    async def _flush_batch(self, items: list[Any]) -> None:
        # ErrorJob 规划阶段就已经确定失败，不碰数据库，直接记报告。
        for item in items:
            if isinstance(item, _ErrorJob):
                self.reports.append((item.identifier, item.report))

        # 同一批里同源的翻页结果先在内存里合并，落库时一个源只查一次去重键、
        # 只推进一次水位——而不是每页各查各的。
        page_messages: dict[uuid.UUID, list[CollectedMessage]] = {}
        page_counts: dict[uuid.UUID, int] = {}
        opaque_items: list[_OpaqueResult] = []
        for item in items:
            if isinstance(item, _PageResult):
                page_messages.setdefault(item.source_id, []).extend(item.messages)
                page_counts[item.source_id] = page_counts.get(item.source_id, 0) + 1
            elif isinstance(item, _OpaqueResult):
                opaque_items.append(item)

        if not page_messages and not opaque_items:
            return

        source_ids = set(page_messages) | {item.source.id for item in opaque_items}
        async with self._sessionmaker() as session:
            try:
                rows = list(await session.scalars(select(Source).where(Source.id.in_(source_ids))))
                sources_by_id = {s.id: s for s in rows}

                for source_id, messages in page_messages.items():
                    source = sources_by_id.get(source_id)
                    if source is None:  # 源在这期间被删了，落库无意义
                        continue
                    created, duplicated, skipped = await _ingest_messages(session, source, messages)
                    source.total_backfilled += created
                    self.page_totals.pages += page_counts[source_id]
                    self.page_totals.created += created
                    self.page_totals.duplicated += duplicated
                    self.page_totals.skipped += skipped

                for item in opaque_items:
                    await self._consume_opaque(session, sources_by_id.get(item.source.id), item)

                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _consume_opaque(
        self, session: AsyncSession, source: Source | None, item: _OpaqueResult
    ) -> None:
        report = item.report
        if source is None:
            self.reports.append((item.source.identifier, report))
            return

        if report.error is None:
            created, duplicated, skipped = await _ingest_messages(
                session, source, item.fetch_messages
            )
            report.created += created
            report.duplicated += duplicated
            report.skipped_empty += skipped

            b_created, b_duplicated, b_skipped = await _ingest_messages(
                session, source, item.backfill_messages
            )
            report.backfill_created += b_created
            report.duplicated += b_duplicated
            report.skipped_empty += b_skipped

            for name in _OPAQUE_COPY_FIELDS:
                setattr(source, name, getattr(item.source, name))
            source.total_collected += report.created
            source.total_backfilled += report.backfill_created
        else:
            source.consecutive_failures = item.source.consecutive_failures
            source.last_error = item.source.last_error
            source.next_fetch_at = item.source.next_fetch_at

        self.reports.append((source.identifier, report))


def _pipeline_counts(pipeline: Pipeline) -> tuple[int, int]:
    """(累计入队总数, 累计已提交总数)。

    入队总数取生产者写入第一条队列的历史累计（`BaseProducer.stats()['produced']`），
    随生产者规划出更多任务（尤其是 Telegram 补历史拆出来的翻页任务）动态增长。
    已提交总数取 `BaseBatchConsumer.stats()['consumed']`——`BaseBatchConsumer`
    只在 `consume_batch` 跑完之后才按整批累加这个计数，语义上就是"落库成功的
    条目数"，不是"进了消费者缓冲区的条目数"（那是手搓版本才需要担心的问题，
    见模块 docstring）。
    """
    total = pipeline.producer.stats()["produced"]
    consumer_stats = pipeline.consumer.stats() if pipeline.consumer is not None else None
    if consumer_stats is not None:
        done = consumer_stats["consumed"]
    else:
        last_stage = pipeline.stages[-1].stats()
        done = last_stage["processed"] + last_stage["failed"]
    return total, done


def _pipeline_pending(pipeline: Pipeline) -> int:
    """两条队列里当前还没被取走的积压条数，用来判断"是否已经彻底跑空"。

    只数各队列自己的 `qsize()`，不代表"正在某个线程里处理中"的条目——那部分
    交给 `pipeline.stop()` 自带的 `Queue.join()` 兜底等待，这里只是决定什么
    时候可以放心地从"轮询进度"切到"调用 stop() 收尾"。
    """
    pending = pipeline.producer.stats()["output_qsize"]
    if pipeline.consumer is not None:
        pending += pipeline.consumer.stats()["input_qsize"]
    else:
        output_qsize = pipeline.stages[-1].stats()["output_qsize"]
        pending += output_qsize or 0
    return pending


#: 生产者->处理单元 这条队列（装等待抓取的翻页/整源任务）的容量上限。Telegram
#: 补历史一次规划就能拆出上千个翻页任务，队列不限容量的话，生产者会远远跑在
#: 实际抓取速度前面——不但抓取并发一下子冲太高容易被 Telegram 限流，规划阶段
#: 又是乐观提交游标（见模块 docstring），堆积越多，中途异常时被跳过、水位却
#: 已经往前跳过的翻页任务就越多。塞满就让生产者线程阻塞在 `put()` 上，等
#: 处理单元消化掉一些再继续规划下一批，形成背压。
_QUEUE_MAXSIZE = 2000

#: 一次 collect 最多累计规划这么多个任务就收工（见 `run_collect_pipeline`
#: 的 `limit` 参数）——跟 `_QUEUE_MAXSIZE` 是两回事：那个限的是队列瞬时
#: 积压，这个限的是整次运行的累计总量，避免进度条上的分母（累计规划数）
#: 无限往上跑。
_DEFAULT_LIMIT = 2000


def run_collect_pipeline(
    *,
    source_id: uuid.UUID | None = None,
    batch_size: int = 500,
    write_batch: int = 100,
    flush_interval: float = 10.0,
    concurrency: int = 4,
    queue_maxsize: int = _QUEUE_MAXSIZE,
    limit: int | None = _DEFAULT_LIMIT,
    settings: Settings | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> CollectPipelineResult:
    """跑一次完整的生产者/处理单元/消费者流水线，返回按源汇总的报告。

    同步阻塞函数——funworker 本身是阻塞式设计，不需要外层 `asyncio.run`
    包装。生产/消费两端各自持有专属的 `AsyncEngine` + 事件循环（见模块
    docstring），处理单元线程池并发数由 `concurrency` 控制，多个源（含
    Telegram 拆出来的并发翻页任务）共享同一条队列。消费者最多攒
    `write_batch` 条或每 `flush_interval` 秒批量落库一次，取先满足的那个
    条件——funworker 的 `Pipeline` 只让处理单元这一段并发，消费者永远是单
    线程，逐条提交会让落库耗时线性堆在这一个线程上，抵消掉抓取侧的并发
    收益。

    `queue_maxsize` 给生产者->处理单元 那条队列设容量上限（见
    `_QUEUE_MAXSIZE`），满了生产者就阻塞在写入上，防止规划任务的速度远超
    实际抓取/落库速度，既避免并发抓取过猛触发限流，也避免堆积过多"游标已经
    乐观提交、任务却还没被消费"的翻页任务。这只限制"同一时刻还没被消费的
    积压"，不限制一次 collect 下来累计规划的总数——累计数完全可能远超
    `queue_maxsize`，因为队列消费一批、生产者又补一批，进度条上的分母
    （`_pipeline_counts` 里的 `total`）是 `producer.stats()["produced"]`，
    这是个跨整次运行单调递增的累计计数，不是队列瞬时深度。

    `limit` 才是管累计总数的开关：一次 collect 最多规划这么多个任务
    （Telegram 单页翻页任务、其它源的整源任务都算）就收工，扫描到一半的源
    留给下次 collect 继续（`_last_id` 只存在这次运行的内存里，不持久化，
    不会丢）。默认 `_DEFAULT_LIMIT`，适合"跑一次别刷太猛，配合外部定时任务
    分批把存量慢慢啃完"这种用法；传 `None` 关掉，一次性扫完所有到期的源。

    `on_progress(total_enqueued, total_done)` 每 0.5 秒轮询一次，即便生产者
    已经跑完（规划本身通常比 HTTP 抓取快得多），只要队列里还有积压就继续
    轮询——不然进度条会在处理单元/消费者还在忙的时候看起来像卡死了。
    """
    settings = settings or get_settings()

    pipeline = Pipeline.build(
        _CollectProducer,
        _CollectProcessor,
        _CollectConsumer,
        num_workers=max(1, concurrency),
        input_maxsize=queue_maxsize,
        producer_kwargs={
            "settings": settings,
            "source_id": source_id,
            "batch_size": batch_size,
            "limit": limit,
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
    assert isinstance(consumer, _CollectConsumer)
    return CollectPipelineResult(reports=consumer.reports, backfill_pages=consumer.page_totals)
