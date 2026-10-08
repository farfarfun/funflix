"""校验编排：限流 → 探测 → 落库 → 排下次复查。"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from farlog import getLogger
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.backoff import backoff
from funflix.base.enums import CheckStatus, Provider
from funflix.models import LinkCheck, Resource, utcnow
from funflix.services.counters import refresh_for_resource
from funflix.services.verify.base import CheckOutcome, LinkProbe, LinkRef
from funflix.services.verify.registry import get_probe

logger = getLogger("funflix")

#: 各状态的复查间隔，见 docs/DESIGN.md §6.4
_RECHECK_TTL: dict[CheckStatus, timedelta | None] = {
    CheckStatus.VALID: timedelta(days=7),
    # 失效的再确认一次；连续两次失效就不再复查（下面按 attempts 判定）
    CheckStatus.INVALID: timedelta(days=30),
    # 缺提取码不会自己好，等人工补码，不自动复查
    CheckStatus.NEED_PASSWORD: None,
    CheckStatus.UNSUPPORTED: None,
}

#: 连续这么多次判定失效后，不再浪费请求
_INVALID_CONFIRM_TIMES = 2

#: 按网盘覆盖限速（次/秒），覆盖 `--rate` 给的全局值。只写**实测扛不住全局速率**
#: 的网盘，没列进来的沿用全局值。
#:
#: 阿里云盘在 `--rate 5.0`（默认值）下实测 300 条里 238 条返回
#: `{"code":"TooManyRequests"}`，生产库因此积压了 14,284 条 `rate_limited` 资源。
#: 对同一批 25 条阿里分享按不同速率各探一轮：
#:
#: | 速率    | invalid | valid | rate_limited |
#: |---------|---------|-------|--------------|
#: | 2.0 次/秒 | 13      | 1     | **11**       |
#: | 1.0 次/秒 | 21      | 1     | **2**        |
#: | 0.5 次/秒 | 23      | 1     | **0**        |
#:
#: 取 1.0 而不是 0.5：被限流的响应不会误判成失效（`INVALID` 要求明确的业务码），
#: 只是白跑一次、排退避重试，所以这里要最大化的是**单位时间内探出结论的条数**。
#: 1.0 次/秒 × 92% ≈ 0.92 条/秒，优于 0.5 次/秒 × 100% = 0.5 条/秒。
#:
#: 夸克在 5.0 次/秒下没有限流迹象（生产库 10,980 valid / 6,585 invalid），
#: 所以是按网盘覆盖，而不是把全局速率调慢 —— 夸克才是队列里的大头。
#:
#: **这张表现在只是下限（最快允许多快），不是目标值。** 上面那 8% 是 25 条样本
#: 推出来的，长跑完全不是这个数：run 37706256433 整轮 80 分钟全是阿里在跑
#: （夸克那轮没有到期的），3,657 次调用里 1,339 次被限流（33%），折算每小时
#: ~2,440 次。差别是**持续量** —— 25 条请求在任何小时级配额内都无感，长跑会
#: 把它顶穿。配额看不见也会变，所以实际间隔交给 `_IntervalTable` 按限流反馈
#: 自己收敛，这里只保证"不会比这更快"。
PROVIDER_RATE_LIMITS: dict[Provider, float] = {
    Provider.ALIPAN: 1.0,
}

#: 吃到一次限流，就把该网盘的请求间隔乘上这个系数。
_PENALTY_FACTOR = 1.5

#: 该网盘给出一条明确结论，就把间隔乘上这个系数往回收。
#:
#: 跟 `_PENALTY_FACTOR` 一起决定收敛到的限流率：乘性拉长 / 乘性收回在
#: `p·ln(1.5) + (1-p)·ln(0.99) = 0` 处平衡，解出 p ≈ 2.4%，即稳态下约四十条
#: 里才白打一条。恢复调快（比如 0.95）会让它在更高的限流率上平衡。
_RECOVERY_FACTOR = 0.99

#: 间隔最多放大到基准值的多少倍。
#:
#: 封顶不是怕慢，是怕一段网络抖动把某个网盘永久摁死 —— 16 倍对阿里云盘是
#: 16 秒一次，已远低于任何合理配额，再慢只能说明问题不在频次。
_MAX_INTERVAL_FACTOR = 16.0


def _interval_table(
    rate_per_second: float, overrides: dict[Provider, float] | None
) -> tuple[float, dict[Provider, float]]:
    """把"全局速率 + 按网盘覆盖"折算成"全局间隔 + 按网盘间隔"。

    覆盖值与全局值独立：`rate_per_second=0`（不限流）不会解除覆盖 —— 覆盖表里
    的网盘是**实测会被风控**的，这是正确性下限，不是调优参数。要彻底关掉，
    显式传 `overrides={}`。

    Args:
        rate_per_second: 全局速率（次/秒），小于等于 0 表示不限流。
        overrides: 按网盘覆盖的速率；`None` 表示用 `PROVIDER_RATE_LIMITS`，
            传空字典可显式关掉覆盖。

    Returns:
        `(默认间隔秒数, {网盘: 间隔秒数})`，间隔为 0 即该网盘不限流。
    """

    def to_interval(rate: float) -> float:
        return 1.0 / rate if rate > 0 else 0.0

    table = PROVIDER_RATE_LIMITS if overrides is None else overrides
    return to_interval(rate_per_second), {p: to_interval(r) for p, r in table.items()}


class _IntervalTable:
    """按网盘算的请求间隔，带限流反馈：吃到限流就拉长，探出结论就慢慢收回。

    为什么要自适应、而不是把 `PROVIDER_RATE_LIMITS` 里的常数再调小一档 ——
    那个常数没法定准。理由见那张表下面补的那段：同一个 1.0 次/秒，25 条样本
    测出来 8% 限流，线上长跑是 33%。配额是看不见的、会随时间和账号变，再猜
    一个数只是把同样的错往小挪一点。这里换成按反馈收敛：被限流说明打太快，
    拉长间隔；拿到明确结论说明这个节奏网盘认，慢慢收回去。收敛点由
    `_PENALTY_FACTOR` / `_RECOVERY_FACTOR` 决定，约 2.4% 限流率。

    `PROVIDER_RATE_LIMITS` 继续当**下限**（最快允许多快），系数只放大不缩小，
    所以自适应永远不会比那张实测表更激进。

    两个限流器实现（`RateLimiter` / `BlockingRateLimiter`）共用这一个类，不是
    图省代码 —— 退避曲线一旦在两处各复制一份就会悄悄分叉，`base/backoff.py`
    的模块 docstring 讲的是同一件事。

    线程安全：`BlockingRateLimiter` 的读者是 funworker 的生产者线程、写者是
    8 个处理单元线程（见 `services/verify/concurrent_runner.py`），所以系数表
    要加锁。锁只圈住几句算术，不跨任何阻塞调用，`RateLimiter` 在协程里用也
    不会把事件循环卡住。
    """

    def __init__(
        self, rate_per_second: float, overrides: dict[Provider, float] | None = None
    ) -> None:
        """按"全局速率 + 按网盘覆盖"建表，参数含义同 `_interval_table`。"""
        self._default, self._base = _interval_table(rate_per_second, overrides)
        self._lock = threading.Lock()
        self._factors: dict[Provider, float] = {}

    def base_interval_for(self, provider: Provider) -> float:
        """该网盘的基准间隔（不含自适应系数），0 表示不限流。"""
        return self._base.get(provider, self._default)

    def interval_for(self, provider: Provider) -> float:
        """该网盘此刻该用的间隔 = 基准间隔 × 自适应系数。"""
        base = self.base_interval_for(provider)
        if base <= 0:
            # 显式不限流的网盘不参与自适应：把速率设成 0 是调用方明确要求别节流
            # （测试、`verify --resource-id` 单条校验），反馈不该把它偷偷变成限流的。
            return 0.0
        with self._lock:
            return base * self._factors.get(provider, 1.0)

    def on_rate_limited(self, provider: Provider) -> None:
        """该网盘回了限流：拉长间隔。"""
        self._scale(provider, _PENALTY_FACTOR)

    def on_conclusive(self, provider: Provider) -> None:
        """该网盘给出了明确结论（valid / invalid / need_password）：往回收一点。

        只认明确结论，**不认 `ERROR`**。连接超时两边都不能证明：既不说明我们
        打太快（网络抖动长一个样），也不说明这个节奏是对的。两边都不投票，
        比猜一边稳 —— 上一轮线上 263 条 error 全是 `ConnectTimeout`，当成限流
        会把阿里无谓地摁到底，当成成功又会在真被硬封时越打越快。
        """
        self._scale(provider, _RECOVERY_FACTOR)

    def _scale(self, provider: Provider, factor: float) -> None:
        if self.base_interval_for(provider) <= 0:
            return
        with self._lock:
            current = self._factors.get(provider, 1.0)
            # 下夹到 1.0：基准值来自实测表，自适应只负责往慢的方向走。
            self._factors[provider] = min(max(current * factor, 1.0), _MAX_INTERVAL_FACTOR)

    def factors(self) -> dict[Provider, float]:
        """各网盘当前的放大系数快照，给收尾日志用（1.0 的不收录）。"""
        with self._lock:
            return {p: f for p, f in self._factors.items() if f > 1.0}


class RateLimiter:
    """每个网盘一个令牌桶。

    探针打的是网盘的私有接口，打太快会触发风控 —— 一旦被限流，
    返回的响应会被误判成"链接失效"，把整库资源误杀。限流是正确性问题，
    不只是礼貌问题。

    各网盘的耐受度差一个数量级，所以速率也是按网盘算的，见
    `PROVIDER_RATE_LIMITS`；实际间隔还会按限流反馈自适应，见 `_IntervalTable`，
    反馈入口是 `self.intervals.on_rate_limited` / `on_conclusive`。
    """

    def __init__(
        self,
        rate_per_second: float = 1.0,
        *,
        overrides: dict[Provider, float] | None = None,
    ) -> None:
        """初始化限流器。

        Args:
            rate_per_second: 每个网盘每秒允许的最大请求数；小于等于 0 时
                不限流。
            overrides: 按网盘覆盖的速率，默认取 `PROVIDER_RATE_LIMITS`。
        """
        self.intervals = _IntervalTable(rate_per_second, overrides)
        self._locks: dict[Provider, asyncio.Lock] = {}
        self._last: dict[Provider, float] = {}

    def _interval_for(self, provider: Provider) -> float:
        return self.intervals.interval_for(provider)

    async def acquire(self, provider: Provider) -> None:
        """按该网盘的令牌桶节奏阻塞等待，直到可以发起下一次请求。

        同一网盘的并发调用通过各自的 `asyncio.Lock` 排队；锁与计时都绑定在
        当前事件循环上，不能跨线程共享同一个实例（跨线程场景见
        `BlockingRateLimiter`）。

        Args:
            provider: 即将请求的网盘类型。
        """
        interval = self._interval_for(provider)
        if interval <= 0:
            return
        lock = self._locks.setdefault(provider, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            elapsed = now - self._last.get(provider, 0.0)
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            self._last[provider] = asyncio.get_running_loop().time()


class BlockingRateLimiter:
    """`RateLimiter` 的线程安全版，给 `verify` 的 funworker 处理单元线程池用。

    `asyncio.Lock`/`asyncio.sleep` 绑定在各自线程的事件循环上，不能跨线程
    共享同一个实例；这里用 `threading.Lock` + `time.monotonic()` 重写同一套
    令牌桶算法，所有处理单元线程共享同一个实例，"每个网盘每秒最多几次请求"
    才是全局生效，不会被并发线程数放大。

    间隔会按限流反馈自适应（见 `_IntervalTable`）。这个类的反馈是**跨线程**
    写入的：生产者线程读间隔决定吐谁，8 个处理单元线程拿到探测结论后回写，
    `_IntervalTable` 内部自己加了锁。
    """

    def __init__(
        self,
        rate_per_second: float = 1.0,
        *,
        overrides: dict[Provider, float] | None = None,
    ) -> None:
        """初始化限流器。

        Args:
            rate_per_second: 每个网盘每秒允许的最大请求数；小于等于 0 时
                不限流。
            overrides: 按网盘覆盖的速率，默认取 `PROVIDER_RATE_LIMITS`。
        """
        self.intervals = _IntervalTable(rate_per_second, overrides)
        self._dict_lock = threading.Lock()
        self._locks: dict[Provider, threading.Lock] = {}
        self._last: dict[Provider, float] = {}

    def _interval_for(self, provider: Provider) -> float:
        return self.intervals.interval_for(provider)

    def _lock_for(self, provider: Provider) -> threading.Lock:
        with self._dict_lock:
            lock = self._locks.get(provider)
            if lock is None:
                lock = threading.Lock()
                self._locks[provider] = lock
            return lock

    def acquire(self, provider: Provider) -> None:
        """按该网盘的令牌桶节奏阻塞等待，直到可以发起下一次请求。

        用 `threading.Lock` + `time.monotonic()` 实现，可在多线程间共享
        同一个实例，使"每个网盘每秒最多几次请求"在全部处理单元线程间
        全局生效。

        Args:
            provider: 即将请求的网盘类型。
        """
        interval = self._interval_for(provider)
        if interval <= 0:
            return
        with self._lock_for(provider):
            now = time.monotonic()
            elapsed = now - self._last.get(provider, 0.0)
            if elapsed < interval:
                time.sleep(interval - elapsed)
            self._last[provider] = time.monotonic()

    def wait_time(self, provider: Provider) -> float:
        """该网盘还要等多久才能发下一次请求，0 表示现在就能发。

        给**严格轮转**的生产端用（见
        `services/verify/concurrent_runner.py::_VerifyProducer.produce`）：轮到哪个
        网盘就等哪个，但下脚之前得先知道要等多久 —— 不然没法跟 `max_seconds` 的
        剩余预算比，一脚踩进 `acquire` 就可能把 job 睡过 GitHub Action 的硬超时，
        连已经探完的结论都看不出跑没跑完。

        只是查看，**不**消费令牌：返回 0 之后还得自己去 `try_acquire`。

        Args:
            provider: 要查的网盘类型。

        Returns:
            还需等待的秒数；0 表示令牌已就绪，或该网盘显式不限流。
        """
        interval = self._interval_for(provider)
        if interval <= 0:
            return 0.0
        with self._lock_for(provider):
            return max(0.0, interval - (time.monotonic() - self._last.get(provider, 0.0)))

    def try_acquire(self, provider: Provider) -> bool:
        """`acquire` 的非阻塞版：令牌就绪就消费掉并返回 True，否则立刻返回 False。

        给**按网盘拆队列**的生产端用（见
        `services/verify/concurrent_runner.py::_VerifyProducer`）：调用方手上有好
        几个网盘的活，一个线程要管全部网盘，不能为了等某一个网盘把自己睡在这里 ——
        阻塞版 `acquire` 放在处理单元线程里正是这么把整条流水线拖慢的。生产端现在
        配 `wait_time` 一起用：先问要等多久、自己决定等不等，再来这里领令牌。

        Args:
            provider: 即将请求的网盘类型。

        Returns:
            True 表示已经占用了这一轮的令牌，调用方应当立即发请求；False 表示
            该网盘还在冷却，**没有**消费任何令牌。
        """
        interval = self._interval_for(provider)
        if interval <= 0:
            return True
        with self._lock_for(provider):
            now = time.monotonic()
            if now - self._last.get(provider, 0.0) < interval:
                return False
            self._last[provider] = now
            return True


@dataclass(slots=True)
class VerifyReport:
    """一次资源校验的结果报告：校验前后的状态、结论细节与耗时。"""

    resource_id: uuid.UUID
    status: CheckStatus
    before: CheckStatus
    detail: str | None = None
    latency_ms: int | None = None

    @property
    def changed(self) -> bool:
        """本次校验结论是否与校验前的状态不同。"""
        return self.status is not self.before


def _next_check_at(resource: Resource, outcome: CheckOutcome):
    """按结论排下次复查。"""
    now = utcnow()

    if outcome.status is CheckStatus.INVALID:
        # 连续多次确认失效后就不再复查了
        if resource.check_attempts >= _INVALID_CONFIRM_TIMES:
            return None
        return now + (_RECHECK_TTL[CheckStatus.INVALID] or timedelta(days=30))

    if outcome.status in {CheckStatus.RATE_LIMITED, CheckStatus.ERROR}:
        # 不是关于链接的结论 —— 退避重试，不要当成失效
        return now + backoff(resource.check_attempts)

    ttl = _RECHECK_TTL.get(outcome.status)
    return now + ttl if ttl else None


async def check_resource(
    session: AsyncSession,
    resource: Resource,
    probe: LinkProbe | None = None,
    limiter: RateLimiter | None = None,
    *,
    prior_status: CheckStatus | None = None,
) -> VerifyReport:
    """校验一条资源，写入历史并更新最新状态。

    Args:
        prior_status: 领取任务前的真实结论。worker 领取时会把 `check_status`
            置成 `checking` 占位，那不是一个结论 —— 拿它跟本次结果比较，
            "连续两次失效"永远算不出来（每轮都被重置成 1），§6.4 里
            "确认两次失效后停止复查"就永远不会触发，失效链接会被无限复查。
            CLI 直接校验单条资源时不传，此时用资源当前状态即可。
    """
    before = resource.check_status
    probe = probe or get_probe(resource.provider)

    if probe is None:
        resource.check_status = CheckStatus.UNSUPPORTED
        resource.next_check_at = None
        return VerifyReport(
            resource_id=resource.id,
            status=CheckStatus.UNSUPPORTED,
            before=before,
            detail=f"没有 {resource.provider.value} 的探针",
        )

    if limiter is not None:
        await limiter.acquire(resource.provider)

    ref = LinkRef(
        provider=resource.provider,
        share_id=resource.share_id,
        url=resource.url,
        passcode=resource.passcode,
    )
    outcome = await probe.check(ref)

    return await persist_check_outcome(
        session, resource, outcome, probe_name=probe.name, prior_status=prior_status
    )


async def persist_check_outcome(
    session: AsyncSession,
    resource: Resource,
    outcome: CheckOutcome,
    *,
    probe_name: str,
    prior_status: CheckStatus | None = None,
) -> VerifyReport:
    """把已经探测出的结论落库，见 `check_resource` 的 `prior_status` 说明。

    从 `check_resource` 里拆出来，好让 funworker 流水线的消费者线程复用同一套
    落库逻辑——处理单元线程只负责跑 `probe.check()`，落库单独在消费者里做。
    """
    before = resource.check_status
    baseline = prior_status if prior_status is not None else before

    now = utcnow()
    # 历史只追加，用于回答"这条链接什么时候挂的"以及
    # "某网盘最近整体失效率是不是异常"——后者是判断探针本身挂了的关键信号。
    # 不写 resource_id——LinkCheck 完全独立存储，只认 (provider, share_id)，
    # 见 models/check.py 顶部说明。
    session.add(
        LinkCheck(
            provider=resource.provider,
            share_id=resource.share_id,
            url=resource.url,
            checked_at=now,
            status=outcome.status,
            http_code=outcome.http_code,
            probe=probe_name,
            detail=outcome.detail,
            latency_ms=outcome.latency_ms,
        )
    )

    if baseline is CheckStatus.CHECKING:
        # 领取前的结论不可知（上一个 worker 崩在了这条上）。
        #
        # 这里**不动** check_attempts：`claim_resources` 重捞时已经替这次崩溃加过一次了
        # （worker/claim.py 的 decide）。两处都加就会重复计数 —— 一次崩溃加一次、
        # 本次判定再加一次，于是「崩溃一次 + 判失效一次」就凑满
        # _INVALID_CONFIRM_TIMES，把一条**实际只探测过一次**的链接永久退休，
        # 而 §6.4 要求的是确认两次失效。
        pass
    else:
        resource.check_attempts = resource.check_attempts + 1 if outcome.status is baseline else 1
    resource.check_status = outcome.status
    resource.last_checked_at = now
    resource.next_check_at = _next_check_at(resource, outcome)
    if outcome.title and not resource.title_raw:
        resource.title_raw = outcome.title[:512]
    if outcome.size_bytes is not None and resource.size_bytes is None:
        resource.size_bytes = outcome.size_bytes
    if outcome.sharer_id:
        resource.sharer_id = outcome.sharer_id[:128]
    if outcome.sharer_name:
        resource.sharer_name = outcome.sharer_name[:128]
    if outcome.sharer_avatar_url:
        resource.sharer_avatar_url = outcome.sharer_avatar_url[:2048]

    # 链接的「可用性」变了，挂着它的作品的 valid_resource_count 就得跟着变。
    # 一条链接可能属于多部作品（合集），所以按 resource 反查全部关联作品。
    #
    # 比较基准用 baseline 而不是 before：worker 领取时 before 已经是 checking
    # 占位，拿它比会让「原本 valid、这次判定失效」算成「没变化」而跳过重算，
    # 计数就永远停在旧值 —— 恰恰是最需要更新的那种情况。
    if (outcome.status is CheckStatus.VALID) != (baseline is CheckStatus.VALID):
        await session.flush()
        await refresh_for_resource(session, resource.id)

    return VerifyReport(
        resource_id=resource.id,
        status=outcome.status,
        before=baseline,
        detail=outcome.detail,
        latency_ms=outcome.latency_ms,
    )
