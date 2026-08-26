"""校验编排：限流 → 探测 → 落库 → 排下次复查。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.backoff import backoff
from funflix.base.enums import CheckStatus, Provider
from funflix.models import LinkCheck, Resource, utcnow
from funflix.services.counters import refresh_for_resource
from funflix.services.verify.base import CheckOutcome, LinkProbe, LinkRef
from funflix.services.verify.registry import get_probe

logger = logging.getLogger(__name__)

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


class RateLimiter:
    """每个网盘一个令牌桶。

    探针打的是网盘的私有接口，打太快会触发风控 —— 一旦被限流，
    返回的响应会被误判成"链接失效"，把整库资源误杀。限流是正确性问题，
    不只是礼貌问题。
    """

    def __init__(self, rate_per_second: float = 1.0) -> None:
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._locks: dict[Provider, asyncio.Lock] = {}
        self._last: dict[Provider, float] = {}

    async def acquire(self, provider: Provider) -> None:
        if self._interval <= 0:
            return
        lock = self._locks.setdefault(provider, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            elapsed = now - self._last.get(provider, 0.0)
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last[provider] = asyncio.get_running_loop().time()


@dataclass(slots=True)
class VerifyReport:
    resource_id: int
    status: CheckStatus
    before: CheckStatus
    detail: str | None = None
    latency_ms: int | None = None

    @property
    def changed(self) -> bool:
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
    #: 判断"本次结论是否延续上一次"的基准。
    baseline = prior_status if prior_status is not None else before
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

    now = utcnow()
    # 历史只追加，用于回答"这条链接什么时候挂的"以及
    # "某网盘最近整体失效率是不是异常"——后者是判断探针本身挂了的关键信号
    session.add(
        LinkCheck(
            resource_id=resource.id,
            checked_at=now,
            status=outcome.status,
            http_code=outcome.http_code,
            probe=probe.name,
            detail=outcome.detail,
            latency_ms=outcome.latency_ms,
        )
    )

    if baseline is CheckStatus.CHECKING:
        # 领取前的结论不可知（比如上一个 worker 崩在了这条上）。
        # 保守地累加而不是重置 —— 宁可早一点停止复查，也不要因为反复重置
        # 让一条早已失效的链接被永远复查下去。
        resource.check_attempts += 1
    else:
        resource.check_attempts = resource.check_attempts + 1 if outcome.status is baseline else 1
    resource.check_status = outcome.status
    resource.last_checked_at = now
    resource.next_check_at = _next_check_at(resource, outcome)
    if outcome.title and not resource.title_raw:
        resource.title_raw = outcome.title[:512]

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
