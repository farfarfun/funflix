"""worker 执行的三类任务：采集、解析、校验。

每类都是同一个骨架：**领取 → 逐条执行 → 归还租约 → 提交**。

两个刻意的选择：

1. **一条一提交**，而不是整批跑完再提交。worker 是长期驻留的进程，
   中途被 kill 是常态；按批提交会把这一批里已经做完的活全部回滚，
   已经花掉的 LLM token 也跟着白花。
2. **租约在成功路径上显式归还，异常路径上交给它自然过期**。
   立刻归还一个刚刚炸掉的任务，只会让它在同一轮里被同一个 worker 立刻重领、
   再炸一次 —— 租约到期（默认 5 分钟）本身就是最省事的退避。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import CheckStatus
from funflix.services.collect.registry import get_collector
from funflix.services.collect.runner import collect_source
from funflix.services.extract.base import Extractor
from funflix.services.extract.registry import default_extractor_for, get_extractor
from funflix.services.extract.runner import parse_document
from funflix.services.verify.registry import get_probe
from funflix.services.verify.runner import RateLimiter, check_resource
from funflix.worker.claim import (
    DEFAULT_LEASE,
    claim_documents,
    claim_resources,
    claim_sources,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BatchReport:
    """一批任务的执行结果。"""

    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    #: 从过期租约里重捞回来的条数。持续不为 0 说明有 worker 在反复中断。
    reclaimed: int = 0
    #: 重捞时已超重试上限、直接置终态的条数。
    abandoned: int = 0

    @property
    def idle(self) -> bool:
        return self.claimed == 0 and self.abandoned == 0

    def summary(self) -> str:
        return (
            f"领取 {self.claimed} 成功 {self.succeeded} 失败 {self.failed} "
            f"重捞 {self.reclaimed} 放弃 {self.abandoned}"
        )


async def _finish(session: AsyncSession, row: Any) -> None:
    """成功路径的收尾：归还租约并提交。"""
    row.lease_until = None
    await session.commit()


async def _abort(session: AsyncSession, kind: str, row_id: Any, exc: Exception) -> None:
    """异常路径的收尾。

    回滚掉这条任务的所有写入，但**不**清租约 —— 让它自然过期。
    过期后重新领取时会被算作"重捞"并计入重试次数，多次崩溃后就会被置终态，
    不会变成一个能把 worker 反复拖垮的毒任务。
    """
    await session.rollback()
    logger.exception("%s 任务异常 id=%s: %s", kind, row_id, exc)


async def run_collect_batch(
    session: AsyncSession,
    *,
    limit: int = 5,
    lease: timedelta = DEFAULT_LEASE,
) -> BatchReport:
    """循环领取、采集，直到到点的源被清空。

    `limit` 是每批领取多少条，不是这次总共处理多少条 —— 队列有多少到点的源
    就处理多少，不设总量上限。
    """
    report = BatchReport()

    while True:
        claimed = await claim_sources(session, limit=limit, lease=lease)
        report.claimed += len(claimed)
        report.reclaimed += claimed.reclaimed
        report.abandoned += claimed.abandoned

        for source in claimed.rows:
            try:
                collector = get_collector(source.source_type)
                result = await collect_source(session, source, collector)
                report.succeeded += int(result.ok)
                report.failed += int(not result.ok)
                await _finish(session, source)
            except Exception as exc:
                report.failed += 1
                await _abort(session, "采集", source.id, exc)

        if not claimed.rows:
            break
    return report


async def run_parse_batch(
    session: AsyncSession,
    *,
    limit: int = 20,
    lease: timedelta = DEFAULT_LEASE,
    extractor: str | None = None,
) -> BatchReport:
    """循环领取、解析，直到待抽取的文本被清空。

    Args:
        extractor: 强制使用某个抽取器。留空则按来源类型自动选 ——
            表格源用 sheet、自由文本用 rule，选错不会报错，只会大批归属失败。
        limit: 每批领取多少条，不是这次总共处理多少条 —— 队列有多少待处理的
            文档就处理多少，不设总量上限。
    """
    report = BatchReport()
    cache: dict[str, Extractor] = {}

    while True:
        claimed = await claim_documents(session, limit=limit, lease=lease)
        report.claimed += len(claimed)
        report.reclaimed += claimed.reclaimed
        report.abandoned += claimed.abandoned

        for doc in claimed.rows:
            try:
                kind = extractor or default_extractor_for(doc.source_type)
                if kind not in cache:
                    # LLM 抽取器在构造时就要读凭证，配置缺失会在这里抛
                    cache[kind] = get_extractor(kind)
                result = await parse_document(session, doc, cache[kind])
                report.succeeded += int(result.ok)
                report.failed += int(not result.ok)
                await _finish(session, doc)
            except Exception as exc:
                # parse_document 自己会吞掉抽取异常并推进状态机，能漏到这里的
                # 基本是抽取器构造失败之类与具体文档无关的问题。
                report.failed += 1
                await _abort(session, "解析", doc.id, exc)

        if not claimed.rows:
            break
    return report


async def run_verify_batch(
    session: AsyncSession,
    *,
    limit: int = 20,
    lease: timedelta = DEFAULT_LEASE,
    limiter: RateLimiter | None = None,
) -> BatchReport:
    """循环领取、校验，直到到点复查的资源被清空。

    `limit` 是每批领取多少条，不是这次总共处理多少条 —— 队列有多少到点复查的
    资源就处理多少，不设总量上限。
    """
    report = BatchReport()

    while True:
        claimed = await claim_resources(session, limit=limit, lease=lease)
        report.claimed += len(claimed)
        report.reclaimed += claimed.reclaimed
        report.abandoned += claimed.abandoned

        for resource in claimed.rows:
            try:
                probe = get_probe(resource.provider)
                result = await check_resource(
                    session,
                    resource,
                    probe,
                    limiter,
                    # 领取时状态已被改成 checking，那只是占位。不把领取前的结论
                    # 传进去，"连续两次失效就停止复查"永远算不出来。
                    prior_status=claimed.priors.get(resource.id),
                )
                # error / rate_limited 不是关于链接的结论，是"没探出来"，算失败。
                inconclusive = result.status in {CheckStatus.ERROR, CheckStatus.RATE_LIMITED}
                report.succeeded += int(not inconclusive)
                report.failed += int(inconclusive)
                await _finish(session, resource)
            except Exception as exc:
                report.failed += 1
                await _abort(session, "校验", resource.id, exc)

        if not claimed.rows:
            break
    return report
