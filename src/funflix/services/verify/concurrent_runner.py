"""`funflix verify` 的并发执行引擎：用 funworker 把生产/处理/消费三段解耦。

结构照抄 `services/extract/concurrent_runner.py`：生产者线程翻页读 `Resource`，
处理单元线程池并发跑 `probe.check()`（网络 IO），消费者线程批量落库/提交。
`services/verify/runner.py` 里的 `check_resource` 全程单线程跑，继续留给
`worker/tasks.py::run_verify_batch`（常驻 worker 模式，走租约领取，是完全
独立的调用路径）；这里只服务 `funflix verify` 这条一次性批处理 CLI 命令。

处理单元线程只负责 `probe.check()`，不碰数据库——探针（见 `registry.get_probe`）
线程私有缓存一份即可，`_probe_for` 按 provider 缓存，同一线程内的多次
`check()` 复用同一个探针实例，探针内部的 httpx client 也就跟着复用，
省掉每次请求重建 TCP/TLS 连接的开销（实测每次约 1.3s，是校验环节的真正瓶颈，
不是限流）；线程退出时 `on_stop` 显式关掉缓存里每个探针的连接池。

限流**不在**处理单元线程里，而是由生产者按网盘拆队列、各自按自己的速率吐数据
（见 `_VerifyProducer`）。放在处理单元里会出队头阻塞：阿里云盘只扛得住
1 次/秒，线程们会挨个睡在它那把锁上，把夸克（队列里的大头，扛得住 5 次/秒）
一起拖住，整体吞吐退化成最慢那个网盘的速率。用的是同步的
`BlockingRateLimiter` 而不是 `asyncio.Lock` 版的 `RateLimiter`——后者绑在
各自线程的事件循环上，不能跨线程用。

速率本身不是写死的常数，而是按限流反馈收敛的：处理单元拿到探测结论后立刻
回写给同一个限流器实例（`_VerifyProcessor._feed_back`），被限流就拉长该网盘的
间隔、拿到明确结论就慢慢收回，`PROVIDER_RATE_LIMITS` 只当下限。原因是实测
常数定不准：阿里云盘 1.0 次/秒在 25 条样本上只有 8% 被限流，线上长跑
（run 37706256433，3,657 次调用）是 33%，差的是持续量顶穿了小时级配额。

落库逻辑（写 `LinkCheck`、推进 `resource.check_status`/`next_check_at`、
刷新作品的 `valid_resource_count`）留给消费者线程，复用
`services/verify/runner.py::persist_check_outcome`。

进度用 `on_progress(total_enqueued, total_committed)` 轮询喂给调用方：分母是
"入队列的总数"（随生产者翻页动态增长），分子是"消费者真正提交到数据库的
资源数"。`_VerifyConsumer` 继承 `funworker.BaseBatchConsumer`，分子用的是
它自带的 `stats()["consumed"]`——只在 `consume_batch` 跑完之后才按整批累加，
不用处理单元线程池的 `processed`/普通 `BaseConsumer.consumed`（逐条累加），
理由同 `services/extract/concurrent_runner.py` 模块 docstring。

轮询循环的收尾信号跟进度显示分开算：用 `_pipeline_pending` 看两条队列的
qsize 是否都归零，不用"提交数追上入队总数"——后者在样本量小、或落库比探测
慢时可能永远追不上，会让循环卡死，理由同
`services/extract/concurrent_runner.py` 模块 docstring。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from farlog import getLogger
from funworker import SKIP, BaseBatchConsumer, BaseProcessor, BaseProducer, Pipeline
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from funflix.base.config import Settings, get_settings
from funflix.base.db import create_engine
from funflix.base.dbconflict import retry_on_write_conflict
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


#: 所有网盘的令牌都没就绪时，生产者空转一轮前睡多久（秒）。
#:
#: 睡得短是为了两件事：吐出时机跟令牌就绪时刻的偏差不超过这个值（最慢的网盘
#: 也是 1 次/秒，50ms 的粒度够用），以及 `BaseProducer._loop` 能及时看到停止信号 ——
#: 睡在 `produce()` 里的时间是不响应 `stop()` 的。
_IDLE_SLEEP = 0.05

#: 算"网盘确实答复了我们"的结论，用来给限流器投赞成票。
#:
#: `UNSUPPORTED` 不在里面：那是没探针、根本没发请求，不构成任何速率证据。
#: `ERROR` 也不在，理由见 `runner._IntervalTable.on_conclusive`。
_CONCLUSIVE = frozenset({CheckStatus.VALID, CheckStatus.INVALID, CheckStatus.NEED_PASSWORD})

logger = getLogger("funflix")


class _VerifyProducer(BaseProducer):
    """**按网盘拆队列**：每个网盘一条独立的翻页游标 + 一个本地缓冲区，
    由各网盘自己的令牌桶决定下一条吐谁。

    为什么不是一条共享队列 —— 限流是按网盘算的（见
    `runner.PROVIDER_RATE_LIMITS`），而阿里云盘只扛得住 1 次/秒、夸克扛得住
    5 次/秒。共享队列 + 在**处理单元线程里**阻塞限流的话，8 个线程会挨个卡在
    阿里那把锁上睡觉，排在后面的夸克链接（队列里的大头）只能干等：实测 300 条
    以阿里为主的资源跑了 5 分 05 秒，整条流水线被压到 1 条/秒 —— 等于整体吞吐
    被**最慢的那个网盘**决定。

    拆开之后限流挪到生产端，而且用的是非阻塞的 `try_acquire`：哪个网盘的令牌
    就绪就吐哪个，都没就绪才让生产者自己睡 `_IDLE_SLEEP`。处理单元线程因此
    永远在做真正的网络请求，整体吞吐变成**各网盘速率之和**。

    还有一个副作用是必须的：翻页游标也得按网盘各自一条。共享一条游标时，
    「下一页」是按全局 `(last_checked_at, id)` 取的，一页里可能全是阿里 ——
    那夸克的缓冲区就一直是空的，拆队列也白拆。
    """

    def __init__(
        self,
        output_queue: Any,
        *,
        settings: Settings,
        limit: int | None,
        batch_size: int,
        recheck_all: bool,
        rate_limiter: BlockingRateLimiter,
        max_seconds: float | None = None,
        name: str | None = None,
    ) -> None:
        """初始化生产者。

        Args:
            output_queue: funworker 的输出队列，翻页读到的待校验资源经此发给
                处理单元线程池。
            settings: 数据库等运行配置，`on_start` 建立专属引擎时使用。
            limit: 本次最多产出的资源条数；None 表示不限，翻页到没有更多
                待校验资源为止。
            batch_size: 每个网盘每次翻页查询的条数。
            recheck_all: True 时忽略 `next_check_at`，把所有可校验 provider
                的资源都当成待处理（强制全量复查）；False 时只取到期的。
            rate_limiter: 按网盘限速的令牌桶。这里只用它的非阻塞接口
                `try_acquire`，**不要**换成 `acquire` —— 生产者只有一个线程，
                在这儿睡等某个网盘就把别的网盘也一起堵住了。
            max_seconds: 墙上时间预算（秒），到点就停止产出；None 表示不设。
                跟 `limit` 是两种不同的闸门，**要的是前者**：这条流水线的
                吞吐由网盘限速决定（阿里 1 次/秒），所以"多少条"换算成
                "多少时间"取决于队列里各网盘的占比，没法事先定准。
                GitHub Action 里 job 有硬超时，算错就是整轮被判 cancelled、
                连已经探完的结论都看不出跑没跑完。
            name: 线程名，透传给 `BaseProducer`。
        """
        super().__init__(output_queue, name=name)
        self.settings = settings
        self.limit = limit
        self.batch_size = batch_size
        self.recheck_all = recheck_all
        self.rate_limiter = rate_limiter
        self.max_seconds = max_seconds

    def on_start(self) -> None:
        """线程启动时建立专属事件循环、数据库引擎，并重置每个网盘的翻页游标。"""
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        self._order = list(CHECKABLE_PROVIDERS)
        self._turn = 0
        self._buffers: dict[Provider, list[dict[str, Any]]] = {p: [] for p in self._order}
        self._last_ts: dict[Provider, Any] = {p: None for p in self._order}
        # 全零 UUID 当"比任何真实 UUIDv7 都小"的哨兵，理由同
        # `services/extract/concurrent_runner.py::_ParseProducer.on_start`。
        self._last_id: dict[Provider, uuid.UUID] = {p: uuid.UUID(int=0) for p in self._order}
        self._exhausted: dict[Provider, bool] = {p: False for p in self._order}
        self._remaining_limit = self.limit
        self._deadline = (
            None if self.max_seconds is None else time.monotonic() + max(0.0, self.max_seconds)
        )

    def on_stop(self) -> None:
        """线程退出前释放数据库引擎并关闭事件循环。"""
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def _next_provider(self) -> Provider:
        """轮转取下一个网盘。轮转而不是固定顺序，否则排在前面的网盘会一直抢到名额。"""
        provider = self._order[self._turn % len(self._order)]
        self._turn += 1
        return provider

    def produce(self) -> Any:
        """吐出下一条「所属网盘此刻允许发请求」的待校验资源。

        Returns:
            描述一条待校验资源的字典（resource_id/provider/share_id/url/passcode）；
            还有活但所有网盘都在冷却时返回 `SKIP`，这一轮不往下游塞东西。

        Raises:
            StopIteration: 每个网盘都翻到最后一页、缓冲区也都空了，或者已经吐满
                `limit` 条，或者超了 `max_seconds` 的墙上时间预算。
        """
        if self._deadline is not None and time.monotonic() >= self._deadline:
            # 到点就收工。已经吐出去的那些会被下游正常探完、正常落库 ——
            # 收尾只等队列排空（见 `run_verify_pipeline` 的轮询），不丢结果。
            raise StopIteration
        if self._remaining_limit is not None and self._remaining_limit <= 0:
            raise StopIteration

        # 每个网盘至多试一次：拿不到令牌就换下一个，不在这里等。
        for _ in range(len(self._order)):
            provider = self._next_provider()
            if not self._buffers[provider] and not self._exhausted[provider]:
                self._aio_loop.run_until_complete(self._fetch_page(provider))
            if not self._buffers[provider]:
                continue
            if not self.rate_limiter.try_acquire(provider):
                continue
            if self._remaining_limit is not None:
                self._remaining_limit -= 1
            return self._buffers[provider].pop(0)

        if all(self._exhausted.values()) and not any(self._buffers.values()):
            raise StopIteration

        time.sleep(_IDLE_SLEEP)
        return SKIP

    async def _fetch_page(self, provider: Provider) -> None:
        """给某一个网盘翻一页，填进它自己的缓冲区。

        刻意**不**按 `_remaining_limit` 去削这一页的条数：`limit` 现在是在吐出
        时扣的（见 `produce`），而一页里多读的那些行只是没被吐出去而已 ——
        它们的 `next_check_at` 没动，下一轮照样是待校验的。反过来按剩余额度削页，
        会让最后几轮退化成一条一条查库。
        """
        if self._remaining_limit is not None and self._remaining_limit <= 0:
            return

        now = utcnow()
        async with self._sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(Resource)
                    .where(
                        Resource.provider == provider,
                        *_due_conditions(now, recheck_all=self.recheck_all),
                        keyset_after(
                            Resource.last_checked_at,
                            Resource.id,
                            self._last_ts[provider],
                            self._last_id[provider],
                        ),
                    )
                    # 没有能同时支撑 `provider =` 和这个排序的索引，PG 会扫一遍
                    # 再排序。`resource` 现在 92 万行，实测一页（500 条）
                    # 300~900ms —— 不再是"毫秒级"了，但一页管 500 条、摊到每条
                    # 约 2ms，相比探测本身的 1.3s 可以忽略，仍不值得加索引。
                    # 真要加，索引得是 `(provider, last_checked_at NULLS FIRST, id)`。
                    .order_by(Resource.last_checked_at.nulls_first(), Resource.id)
                    .limit(self.batch_size)
                )
            )
            if not rows:
                self._exhausted[provider] = True
                return

            self._last_ts[provider] = rows[-1].last_checked_at
            self._last_id[provider] = rows[-1].id

            for row in rows:
                self._buffers[provider].append(
                    {
                        "resource_id": row.id,
                        "provider": row.provider,
                        "share_id": row.share_id,
                        "url": row.url,
                        "passcode": row.passcode,
                    }
                )


class _VerifyProcessor(BaseProcessor):
    """并发跑 `probe.check()`，不碰数据库，**也不限流**，但要把限流结论喂回去。

    限流在生产端（见 `_VerifyProducer`）：吐出来的每一条都已经占掉了它所属
    网盘的令牌，拿到就该立刻发请求。这里不能再 `acquire` 一次 —— 一次
    `acquire` 消费一个令牌，两头都收的话实际速率会变成设定值的一半。

    反馈放在这里、而不是放在消费者里，是因为这里**第一手**拿到结论：消费者
    要等攒够 `write_batch` 条或 `flush_interval` 秒才落库，隔着十几秒再回写，
    这期间生产者还在按旧速率往外吐。喂回去的是 `rate_limiter`（所有线程共享
    的同一个实例），由它按网盘调间隔，见 `runner._IntervalTable`。
    """

    def __init__(self, *args: Any, rate_limiter: BlockingRateLimiter, **kwargs: Any) -> None:
        """初始化处理单元。

        Args:
            rate_limiter: 生产端那一个限流器实例，这里**只回写反馈、不取令牌**。
        """
        super().__init__(*args, **kwargs)
        self.rate_limiter = rate_limiter

    def on_start(self) -> None:
        """线程启动时建立专属事件循环，并初始化本线程的探针缓存。"""
        self._aio_loop = asyncio.new_event_loop()
        self._probe_cache: dict[Provider, LinkProbe | None] = {}

    def on_stop(self) -> None:
        """线程退出前关闭本线程缓存的每个探针持有的 HTTP 连接池。"""
        # 探针的 httpx client 在 on_start 之后惰性建、线程存活期内一直复用
        # （见 base.py::AnonymousHttpProbe），线程退出前得显式关掉，不然连接池泄漏。
        for probe in self._probe_cache.values():
            aclose = getattr(probe, "aclose", None)
            if aclose is not None:
                self._aio_loop.run_until_complete(aclose())
        self._aio_loop.close()

    def _probe_for(self, provider: Provider) -> LinkProbe | None:
        if provider not in self._probe_cache:
            self._probe_cache[provider] = get_probe(provider)
        return self._probe_cache[provider]

    def process(self, item: dict[str, Any]) -> Any:
        """对一条待校验资源执行探测（限流已在生产端做完）。

        没有对应探针时直接判 UNSUPPORTED，不发起请求。探针 `check` 本身已经
        兜底了自己的异常，这里再加一层防御性 try/except，防止未来新探针
        漏掉兜底时把整条流水线拖垮。

        Args:
            item: `_VerifyProducer.produce` 吐出的待校验资源字典。

        Returns:
            包含 `resource_id`、探测结论 `outcome`、探针标识 `probe_name` 的字典。
        """
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

        ref = LinkRef(
            provider=provider, share_id=item["share_id"], url=item["url"], passcode=item["passcode"]
        )
        try:
            outcome = self._aio_loop.run_until_complete(probe.check(ref))
        except Exception as exc:
            # 探针骨架本身已经兜底了自己的异常，这层纯防御性——防止未来新探针
            # 实现漏掉兜底时，一次异常把整条流水线拖垮。
            outcome = CheckOutcome(status=CheckStatus.ERROR, detail=f"{type(exc).__name__}: {exc}")
        self._feed_back(provider, outcome.status)
        return {"resource_id": item["resource_id"], "outcome": outcome, "probe_name": probe.name}

    def _feed_back(self, provider: Provider, status: CheckStatus) -> None:
        """把这一次的结论喂给限流器，让它调整该网盘的节奏。

        `ERROR` 不投票，理由见 `runner._IntervalTable.on_conclusive`。
        """
        if status is CheckStatus.RATE_LIMITED:
            self.rate_limiter.intervals.on_rate_limited(provider)
        elif status in _CONCLUSIVE:
            self.rate_limiter.intervals.on_conclusive(provider)


class _VerifyConsumer(BaseBatchConsumer):
    """攒够 `write_batch` 条，或距上次落库过了 `flush_interval` 秒（或流水线收尾时），批量落库一次。

    继承 `funworker.BaseBatchConsumer`，攒批/计时轮询逻辑交给基类，这里只
    实现 `consume_batch`，理由同
    `services/extract/concurrent_runner.py::_ParseConsumer`。
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
        """初始化消费者。

        Args:
            input_queue: funworker 的输入队列，处理单元的探测结果经此发来。
            settings: 数据库等运行配置，`on_start` 建立专属引擎时使用。
            write_batch: 攒够多少条触发一次批量落库。
            flush_interval: 距上次落库超过多少秒也触发一次批量落库（秒），
                即便还没攒够 `write_batch` 条。
            name: 线程名，透传给 `BaseBatchConsumer`。
        """
        super().__init__(
            input_queue, batch_size=write_batch, batch_timeout=flush_interval, name=name
        )
        self.settings = settings
        self.reports: list[VerifyReport] = []

    def on_start(self) -> None:
        """线程启动时建立专属事件循环和数据库引擎。"""
        self._aio_loop = asyncio.new_event_loop()
        self._engine = create_engine(self.settings)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )

    def on_stop(self) -> None:
        """线程退出前释放数据库引擎并关闭事件循环。"""
        self._aio_loop.run_until_complete(self._engine.dispose())
        self._aio_loop.close()

    def consume_batch(self, items: list[dict[str, Any]]) -> None:
        """批量落库一批探测结果（同步入口，内部转发给异步实现）。

        Args:
            items: `_VerifyProcessor.process` 产出的结果字典列表。
        """
        self._aio_loop.run_until_complete(self._flush_batch(items))

    async def _flush_batch(self, items: list[dict[str, Any]]) -> None:
        """落一批，撞上并发写入冲突时重试几次。

        `resource` 同时被别的节点写（CI 里 parse 在重建资源、`db relink-checks`
        在按批回填历史结论），行锁的加锁顺序跟我们这批不一致就会死锁。
        实测一轮 323 条里有 2 批（40 条）栽在这上面，异常直接抛出去被
        `BaseConsumer._loop` 吞掉记个 traceback —— 那 40 条探测白做了（行还是
        UNCHECKED，下轮重新探一遍）。

        重跑是干净的：`_flush_once` 每次自己开一个新会话，上一次失败的事务
        随 `async with` 退出就回滚掉了，不用操心 aborted 状态。
        """
        await retry_on_write_conflict(
            lambda: self._flush_once(items), what=f"这批 {len(items)} 条校验结论"
        )

    async def _flush_once(self, items: list[dict[str, Any]]) -> None:
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
    """(总入队数, 总提交数)，理由同 `services/extract/concurrent_runner.py::_pipeline_counts`。"""
    total = pipeline.producer.stats()["produced"]
    done = pipeline.consumer.stats()["consumed"]
    return total, done


def _pipeline_pending(pipeline: Pipeline) -> int:
    """理由同 `services/extract/concurrent_runner.py::_pipeline_pending`。"""
    pending = pipeline.producer.stats()["output_qsize"]
    if pipeline.consumer is not None:
        pending += pipeline.consumer.stats()["input_qsize"]
    return pending


def run_verify_pipeline(
    *,
    limit: int | None = None,
    batch_size: int = 500,
    write_batch: int = 20,
    flush_interval: float = 10.0,
    concurrency: int = 8,
    rate: float = 5.0,
    recheck_all: bool = False,
    max_seconds: float | None = None,
    settings: Settings | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[VerifyReport]:
    """跑一次完整的生产者/处理单元/消费者流水线，返回消费者攒的全部报告。

    同步阻塞函数——funworker 本身是阻塞式设计，不需要外层 `asyncio.run` 包装。
    生产/消费两端各自持有专属的 `AsyncEngine` + 事件循环（见模块 docstring），
    处理单元线程池并发数由 `concurrency` 控制。限流归生产者一个人管（按网盘
    拆队列，见 `_VerifyProducer`），处理单元拿到就发请求。消费者最多攒
    `write_batch` 条或每 `flush_interval` 秒批量落库一次，取先满足的那个条件。

    `on_progress(total_enqueued, total_done)` 每 0.5 秒轮询一次，理由同
    `services/extract/concurrent_runner.py::run_parse_pipeline`。

    `max_seconds` 是**墙上时间**预算：到点后生产者不再吐新的，但本函数要等
    已经吐出去的全部探完、全部落库才返回，所以实际耗时会略超预算（取决于
    最后一批探测的网络延迟）。设它的时候给外层超时留余量。
    """
    settings = settings or get_settings()
    num_workers = max(1, concurrency)
    # 生产者和全部处理单元线程共享同一个限流器实例：生产者读它决定下一条吐谁，
    # 处理单元拿到探测结论后回写反馈（见 `_VerifyProcessor._feed_back`）。
    # 每个线程各建一个的话，反馈就只影响自己那一份，等于没有反馈。
    limiter = BlockingRateLimiter(rate_per_second=rate)

    pipeline = Pipeline.build(
        _VerifyProducer,
        # 传工厂而不是类：`Pipeline.build` 没有 `processor_kwargs`，要把共享的
        # 限流器交给处理单元只能靠闭包。
        lambda: _VerifyProcessor(rate_limiter=limiter),
        _VerifyConsumer,
        num_workers=num_workers,
        # 输入队列限长。限流挪到生产端之后，「已经占了令牌」和「请求真的发出去」
        # 之间隔着这条队列：队列无界的话生产者会按速率一路把几百条灌进去，
        # 令牌早就花光而请求还排在队里，到达网盘的瞬时速率就跟设定值脱钩了。
        # 卡在 `num_workers * 2`，让生产节奏跟着线程的实际消化速度走。
        input_maxsize=num_workers * 2,
        # 输出队列也限长，否则探测端会把结果一路堆在队列里跑在落库前面：实测
        # 60 秒预算的一轮里生产端准时停了（吐了 323 条），但库里撞上并发写入
        # 锁竞争，落库只跑到 1.3 条/秒，整轮拖到 3 分 35 秒才收尾 —— 时间预算
        # 就形同虚设了。限长之后反压会一级级顶回生产端（满 → 探测线程阻塞在
        # put → 输入队列满 → 生产者卡在 `_put`），到点时未落库的尾巴最多几十条。
        # 代价是整体吞吐被落库速度卡住，但探得比记得快本来就没有意义。
        #
        # 这么卡不会把收尾卡死：`Pipeline.stop()` 是先停生产者、再 `drain` 各级
        # 处理单元、**最后**才给消费者发 STOP，排空期间消费者一直在取数据；
        # 而 `BaseConsumer._loop` 把 `consume` 的异常吞掉只记日志，消费者线程
        # 也不会因为一次落库失败就死掉、把上游永久堵在满队列上。
        output_maxsize=write_batch * 2,
        producer_kwargs={
            "settings": settings,
            "limit": limit,
            "batch_size": batch_size,
            "recheck_all": recheck_all,
            "rate_limiter": limiter,
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

    # 把自适应收敛到哪儿了打出来 —— 这是下一轮调 `PROVIDER_RATE_LIMITS` 下限
    # 唯一可信的依据，比再拿几十条样本手测靠谱。
    for provider, factor in sorted(limiter.intervals.factors().items(), key=lambda kv: -kv[1]):
        base = limiter.intervals.base_interval_for(provider)
        logger.info(
            f"{provider.value} 被限流后自适应放慢到 {base * factor:.2f} 秒一次"
            f"（基准 {base:.2f} 秒，放大 {factor:.1f} 倍）"
        )

    consumer = pipeline.consumer
    assert isinstance(consumer, _VerifyConsumer)
    return consumer.reports
