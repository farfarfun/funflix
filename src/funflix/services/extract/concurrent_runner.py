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
import uuid
from collections.abc import Callable
from typing import Any

from funworker import BaseBatchConsumer, BaseProcessor, BaseProducer, Pipeline
from sqlalchemy import ColumnElement, String, cast, func, or_, select
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

#: 十六进制字符表，分片键的取值空间。小写——两种方言存的 uuid 文本都是小写，
#: 但谓词里仍然套了 `lower()`，不依赖这个巧合。
_HEX_DIGITS = "0123456789abcdef"


def parse_shard(spec: str) -> tuple[int, int]:
    """解析 `--shard` 的 `i/N` 写法，返回 `(序号, 总片数)`。

    Args:
        spec: 形如 `"0/8"` 的字符串，序号从 0 开始、必须小于总片数。

    Returns:
        `(index, total)`。

    Raises:
        ValueError: 格式不对、不是整数、总片数不是正数，或序号越界。
    """
    index_text, _, total_text = spec.partition("/")
    if not _:
        raise ValueError(f"分片要写成 i/N（比如 0/8），收到 {spec!r}")
    try:
        index, total = int(index_text), int(total_text)
    except ValueError:
        raise ValueError(f"分片的序号和总数都得是整数，收到 {spec!r}") from None
    if total < 1:
        raise ValueError(f"总片数至少是 1，收到 {total}")
    if not 0 <= index < total:
        raise ValueError(f"分片序号要落在 [0, {total}) 里，收到 {index}")
    return index, total


def shard_digits(index: int, total: int) -> tuple[str, ...]:
    """第 `index` 片负责的 uuid 末位字符集合。

    16 个十六进制字符按 `% total` round-robin 分摊。`total` 整除 16 时
    （1/2/4/8/16）每片严格等量；不整除时相邻片差一个字符，即最多 6.25% 的
    倾斜——够用了，不值得为此引入不可移植的 SQL 取模。
    """
    return tuple(d for d in _HEX_DIGITS if int(d, 16) % total == index)


def shard_condition(shard: tuple[int, int] | None) -> ColumnElement[bool] | None:
    """把分片切成 SQL 谓词：按 `id` 的**末位十六进制字符**取模分摊。

    为什么是末位字符：主键是 UUIDv7，低位 74 位是随机数（`models/base.py::uuid7`），
    所以末位字符天然均匀——生产库 213 万行实测 16 个桶每桶 6.25%±2%。
    换成高位就全是毫秒时间戳前缀，同一批采集进来的文档会挤在同一片里。

    为什么不用 `lease_until` 领取租约来分活：`worker/claim.py` 那套是**逐行**
    带守卫 UPDATE（它要能分辨"新任务"和"崩溃后重捞的任务"，批量 UPDATE 做不到），
    每条多一次往返。而本机到 RDS 的往返是 131ms、每条文档总共才约 2 次往返，
    加租约等于把单进程吞吐再砍三分之一。分片不写库、零额外往返，而且失败模式
    是安全的：切漏了只会让那些文档**留在 pending**，最后补跑一遍不带 `--shard`
    的就能收干净；重复切则因为各片谓词互斥而不可能发生。

    代价是各片要独立扫索引（谓词不可索引，PG 走有序索引逐条过滤），
    即全表索引扫的工作量放大 `total` 倍。配合 `ix_raw_document_parse_scan`
    这是索引内过滤、不回表，比没索引时每页全表扫 2.6GB 便宜得多。

    Args:
        shard: `(序号, 总片数)`；为 None 或总片数为 1 时返回 None（不加谓词）。

    Returns:
        `id` 末位字符落在本片里的条件；不需要分片时为 None。
    """
    if shard is None:
        return None
    index, total = shard
    if total <= 1:
        return None
    id_text = cast(RawDocument.id, String)
    tail = func.lower(func.substr(id_text, func.length(id_text), 1))
    return tail.in_(shard_digits(index, total))


def _pending_conditions(now: Any, shard: tuple[int, int] | None = None) -> tuple[Any, ...]:
    conditions: list[Any] = [
        RawDocument.parse_status == ParseStatus.PENDING,
        or_(RawDocument.next_parse_at.is_(None), RawDocument.next_parse_at <= now),
    ]
    if (clause := shard_condition(shard)) is not None:
        conditions.append(clause)
    return tuple(conditions)


async def count_pending(
    session: AsyncSession, *, limit: int | None, shard: tuple[int, int] | None = None
) -> int:
    """待解析文档总数，供调用方渲染进度条，不影响流水线本身。

    分了片就只数本片的——否则每个分片进程的进度条分母都是全局待处理量，
    看起来像是谁都没在推进。
    """
    now = utcnow()
    total = int(
        await session.scalar(
            select(func.count()).select_from(RawDocument).where(*_pending_conditions(now, shard))
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
        shard: tuple[int, int] | None = None,
        max_seconds: float | None = None,
        name: str | None = None,
    ) -> None:
        """保存构造参数；数据库引擎、事件循环等资源留到 `on_start()` 里按线程现造。

        Args:
            output_queue: funworker 注入的输出队列，生产的条目从这里喂给处理单元。
            settings: 数据库等配置，用于 `on_start()` 里创建专属 `AsyncEngine`。
            extractor_override: 强制指定抽取器名；为 None 时按文档来源类型选默认值。
            limit: 本次最多处理的文档数；None 表示不限。
            batch_size: 每次翻页读取的文档数上限。
            force: 为 True 时跳过缓存查询，强制让每条文档都重新抽取。
            shard: `(序号, 总片数)`，只处理 id 末位落在本片的文档；None 表示全量。
            max_seconds: 墙上时间预算（秒），到点就不再吐新的；None 表示不设。
                比 `limit` 更适合给 CI 的 job 兜时间 —— 见 `produce`。
            name: 线程名，透传给 `BaseProducer`。
        """
        super().__init__(output_queue, name=name)
        self.settings = settings
        self.extractor_override = extractor_override
        self.limit = limit
        self.batch_size = batch_size
        self.force = force
        self.shard = shard
        self.max_seconds = max_seconds

    def on_start(self) -> None:
        """在生产者线程内创建专属 `AsyncEngine`/事件循环，并初始化翻页游标与缓冲区。"""
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._extractor_cache: dict[str, Extractor] = {}
        self._buffer: list[dict[str, Any]] = []
        self._last_ts: Any = None
        # 全零 UUID 当"比任何真实 UUIDv7 都小"的哨兵，跟旧版用 0 当起始整数游标同理。
        self._last_id: uuid.UUID = uuid.UUID(int=0)
        self._remaining_limit = self.limit
        self._exhausted = False
        # 预算从生产者线程真正开跑算起，不从构造算起：建引擎、开事件循环
        # 都在这之前，把那几秒算进预算等于白送。
        self._deadline = (
            None if self.max_seconds is None else time.monotonic() + max(0.0, self.max_seconds)
        )

    def on_stop(self) -> None:
        """释放本线程专属的 `AsyncEngine` 并关闭事件循环。"""
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def _extractor_for(self, kind: str) -> Extractor:
        if kind not in self._extractor_cache:
            self._extractor_cache[kind] = get_extractor(kind)
        return self._extractor_cache[kind]

    def produce(self) -> Any:
        """吐出一条待处理条目；缓冲区空时先翻一页，翻到底则抛 `StopIteration` 结束生产。

        到点（`max_seconds`）也抛 `StopIteration` 收工。**时间预算比 `limit`
        更适合给 CI 兜底**：`limit` 要人先量出「多少条约等于多少分钟」再填，
        而那个换算一直在飘 —— run 37734962378 四个分片都跑满 5000 条，耗时是
        50 / 68 / 68 / 68 分钟，同样的条数差 35%（文档的难易不一样）。于是
        `limit` 只能往保守的那头填，把 job 窗口的三分之一空着：cron 是每 2
        小时一轮、job 预算也是 120 分钟，6000 条跑 82 分钟就退出，剩下 38
        分钟纯空转。换成时间预算就能把窗口填满，也不用再猜数字。

        收工是安全的：解析按 `write_batch` 逐批提交（见 `_ParseConsumer`），
        已经解析完的不会因为收工而回滚，没排到的下一轮接着领。

        Returns:
            含 `doc_id`/`content`/`extractor_kind`/`cached_output` 的字典，供处理单元消费。

        Raises:
            StopIteration: 数据库里已无更多待解析文档，或超了 `max_seconds`。
        """
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise StopIteration
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
                        *_pending_conditions(now, self.shard),
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

    # 没有 __init__：每个条目用哪个抽取器由生产者解析好写进 `extractor_kind`，
    # 处理单元只按条目取，不需要再持有一份 extractor_override。

    def on_start(self) -> None:
        """在处理单元线程内创建专属事件循环，供同步调用 `extractor.extract()` 用。"""
        self._aio_loop = asyncio.new_event_loop()
        self._extractor_cache: dict[str, Extractor] = {}

    def on_stop(self) -> None:
        """关闭本线程专属的事件循环。"""
        self._aio_loop.close()

    def _extractor_for(self, kind: str) -> Extractor:
        if kind not in self._extractor_cache:
            self._extractor_cache[kind] = get_extractor(kind)
        return self._extractor_cache[kind]

    def process(self, item: dict[str, Any]) -> Any:
        """在处理单元线程里跑一条文本的抽取（命中缓存则改走 `rehydrate()`）。

        Args:
            item: 生产者产出的条目，含 `doc_id`/`content`/`extractor_kind`/`cached_output`。

        Returns:
            含 `doc_id`/`extractor_kind` 的字典：成功时带 `outcome` 与
            `from_cache`，抽取抛异常时带 `error`（`类型名: 消息`）。异常在这里
            就地转成数据而不是向上抛，否则一条畸形文本会掀掉整个工作线程。
        """
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
        """保存构造参数并把攒批策略交给基类；数据库引擎留到 `on_start()` 按线程现造。

        Args:
            input_queue: funworker 注入的输入队列，处理单元的产出从这里送来。
            settings: 数据库等配置，用于 `on_start()` 里创建专属 `AsyncEngine`。
            write_batch: 攒够多少条就触发一次 `consume_batch` 落库。
            flush_interval: 即便未攒够 `write_batch` 条，最多等待多少秒也强制落库一次。
            name: 线程名，透传给 `BaseBatchConsumer`。
        """
        super().__init__(
            input_queue, batch_size=write_batch, batch_timeout=flush_interval, name=name
        )
        self.settings = settings
        self.reports: list[ParseReport] = []

    def on_start(self) -> None:
        """在消费者线程内创建专属 `AsyncEngine`/事件循环，供批量落库用。"""
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._extractor_cache: dict[str, Extractor] = {}

    def on_stop(self) -> None:
        """释放本线程专属的 `AsyncEngine` 并关闭事件循环。"""
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def _extractor_for(self, kind: str) -> Extractor:
        if kind not in self._extractor_cache:
            self._extractor_cache[kind] = get_extractor(kind)
        return self._extractor_cache[kind]

    def consume_batch(self, items: list[dict[str, Any]]) -> None:
        """把攒够的一批处理结果（或缓存命中结果）同步落库。

        Args:
            items: 处理单元产出的条目列表，每条含 `doc_id`/`extractor_kind`，
                以及 `outcome`+`from_cache`（成功）或 `error`（抽取阶段失败）。
        """
        self._aio_loop.run_until_complete(self._flush_batch(items))

    async def _flush_batch(self, items: list[dict[str, Any]]) -> None:
        doc_ids = [it["doc_id"] for it in items]
        async with self._sessionmaker() as session:
            try:
                rows = list(
                    await session.scalars(select(RawDocument).where(RawDocument.id.in_(doc_ids)))
                )
                docs_by_id = {d.id: d for d in rows}

                outcomes: dict[uuid.UUID, Any] = {}
                cached_doc_ids: set[uuid.UUID] = set()
                extraction_errors: dict[uuid.UUID, str] = {}
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
    shard: tuple[int, int] | None = None,
    max_seconds: float | None = None,
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

    `shard=(i, N)` 让本次只处理 id 末位落在第 i 片的文档，供 N 个互不重叠的
    进程并行推进同一个队列（见 `shard_condition`）。**一个进程内把
    `concurrency` 开大并不能提速**：落库全程只有一个 `_ParseConsumer` 线程，
    而远程库场景下瓶颈是每条文档那两次网络往返，不是 `extract()` 的 CPU。
    要提吞吐只能多开进程，而多开进程必须分片，否则各进程的翻页游标从同一处
    起步、把同一批文档重复解析一遍。

    `max_seconds` 是**墙上时间**预算：到点后生产者不再吐新的，但本函数要等
    已经吐出去的全部解析完、全部落库才返回，所以实际耗时会略超预算。设它的
    时候给外层超时留余量。为什么它比 `limit` 更适合 CI，见
    `_ParseProducer.produce`。

    ## 两条队列都是限界的

    funworker 的队列默认 `maxsize=0`（不限），而这条流水线的两端速度差一个
    数量级：处理单元是 8 个线程跑纯 CPU 的规则抽取（`--extra llm` 不开时没有
    网络调用），而消费者只有一个线程、每批 20 条要跟远端库来回，实测
    **1.46 条/s**。不限界的后果有三个，run 37757448206 上三个都发生了：

    - **到点收不了工。** 生产者按 extract 的速度一路塞，110 分钟能把几十万条
      塞进队列；`max_seconds` 到点后它停了，但本函数要等队列排空才返回 ——
      按 1.46 条/s 算要几十小时。于是时间预算形同虚设，job 被外层
      `timeout-minutes: 120` 直接砍掉（四个分片都是"The operation was
      canceled"，没有一个优雅退出过）。
    - **白烧 extract。** 被砍掉时队列里那几十万条已经抽取完了，全在内存里，
      随进程一起没了，下一轮要从头再抽一遍。
    - **内存无上限。** 条目里带着 `doc.content` 全文。

    限界之后生产者会卡在 `_put` 上（funworker 的 `_put` 是带超时轮询的阻塞
    写，并且尊重停止信号，不会把停止请求堵死），于是整条流水线被最慢的那一
    段自然限速：在飞的条目不超过两条队列的容量之和，到点后的收尾只要几十秒。
    容量按两端各自的工作单元算 —— 入口要够喂满 `concurrency` 个线程，出口要
    够让消费者随时攒满一个 `write_batch`，各给 4 倍余量。调小它不会降吞吐：
    瓶颈本来就在消费者那一侧，处理单元迟早要等。

    `services/verify/concurrent_runner.py` 和 `collect` 那条早就限界了，而且
    verify 的注释里记的是同一句结论（"时间预算就形同虚设了"）—— 三条流水线
    是一个形状，再加新的流水线时记得一起限上。
    """
    settings = settings or get_settings()

    def processor_factory() -> _ParseProcessor:
        """构造一个 `_ParseProcessor` 实例，供 `Pipeline.build` 为每个线程池工作线程各建一个。"""
        return _ParseProcessor()

    pipeline = Pipeline.build(
        _ParseProducer,
        processor_factory,
        _ParseConsumer,
        num_workers=max(1, concurrency),
        input_maxsize=max(4 * max(1, concurrency), 2 * write_batch),
        output_maxsize=max(4 * write_batch, 4 * max(1, concurrency)),
        producer_kwargs={
            "settings": settings,
            "extractor_override": extractor_name,
            "limit": limit,
            "batch_size": batch_size,
            "force": force,
            "shard": shard,
            "max_seconds": max_seconds,
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
