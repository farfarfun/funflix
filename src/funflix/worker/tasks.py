"""worker 执行的三类任务：采集、解析、校验。

每类都是同一个骨架：**领取一批 → 逐条执行 → 归还租约 → 攒够 `write_batch` 条批量提交**。

两个刻意的选择：

1. **批量提交**，而不是逐条提交。逐条 commit 意味着每条任务后面都跟一次
   数据库往返，在远程数据库上这个延迟会直接叠加到总耗时里；攒够
   `write_batch`（默认 100）条再提交一次，把这部分延迟摊薄。代价是
   worker 中途被 kill 时会丢这一撮里已完成但未提交的工作（连带白花的
   LLM token / 探测次数）——重新跑一次的成本被认为远低于逐条提交的
   往返开销，见 `Settings.worker_write_batch`。真正需要保护的"已经确定
   领过这条任务"状态在 `claim_documents`/`claim_resources`/`claim_sources`
   领取时就已单独提交（见 `worker/claim.py`），不受这里的批量提交影响。
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
from funflix.services.extract.runner import parse_batch
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


async def _mark_done(row: Any) -> None:
    """成功路径：归还租约。不在这里提交——由外层攒够 `write_batch` 条再统一提交。"""
    row.lease_until = None


async def _abort(session: AsyncSession, kind: str, row_id: Any, exc: Exception) -> None:
    """异常路径的收尾。

    回滚这个事务里迄今为止所有未提交的写入 —— 不只是这一条，还包括同一批里
    刚处理完、还没攒够 `write_batch` 就被提交的其它几条。这是批量提交本身
    换来的代价（见模块 docstring），接受即可：那几条任务的状态仍是
    claim 时写入的 running，租约到期后会被当作"重捞"自然重跑。
    不清租约 —— 让它自然过期。过期后重新领取时会被算作"重捞"并计入重试
    次数，多次崩溃后就会被置终态，不会变成一个能把 worker 反复拖垮的毒任务。
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

    不套用模块 docstring 里"攒够 write_batch 条再提交"的批量提交：一个源
    可能翻上百页、跑好几分钟，批量提交会让这么长一段时间的水位推进都压在
    同一个未提交事务里 —— 中途一个源抛异常，连累同批里刚处理完、还没提交
    的其它源一起回滚，表现为水位卡在原地不动。`collect_source` 内部已经
    按 `CommitBatcher` 的节奏（攒够 100 条或过了 1 分钟）自行提交，这里
    只需在它返回后落一次 `_mark_done`；一个源出错只丢它自己这次未提交的
    尾巴，不牵连其它源。
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
                await _mark_done(source)
                await session.commit()
                report.succeeded += int(result.ok)
                report.failed += int(not result.ok)
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
    write_batch: int = 100,
) -> BatchReport:
    """循环领取、解析，直到待抽取的文本被清空。

    Args:
        extractor: 强制使用某个抽取器。留空则按来源类型自动选 ——
            表格源用 sheet、自由文本用 rule，选错不会报错，只会大批归属失败。
        limit: 每批领取多少条，不是这次总共处理多少条 —— 队列有多少待处理的
            文档就处理多少，不设总量上限。
        write_batch: 每 `parse_batch` 调用处理多少条文档再提交一次。也是
            `parse_batch` 内部批量预读 media/resource/tag 去重键的粒度——
            见 `services/extract/runner.py` 模块 docstring：单条 SELECT 在
            远程数据库上一次就要 ~100~150ms，批量预读把这类查询从
            "每条文档一次往返"折叠成"每批几次往返"。
    """
    report = BatchReport()
    cache: dict[str, Extractor] = {}

    while True:
        claimed = await claim_documents(session, limit=limit, lease=lease)
        report.claimed += len(claimed)
        report.reclaimed += claimed.reclaimed
        report.abandoned += claimed.abandoned

        groups: dict[str, list[Any]] = {}
        for doc in claimed.rows:
            kind = extractor or default_extractor_for(doc.source_type)
            groups.setdefault(kind, []).append(doc)

        for kind, docs in groups.items():
            if kind not in cache:
                # LLM 抽取器在构造时就要读凭证，配置缺失会在这里抛
                cache[kind] = get_extractor(kind)
            for i in range(0, len(docs), write_batch):
                chunk = docs[i : i + write_batch]
                try:
                    results = await parse_batch(session, chunk, cache[kind])
                except Exception as exc:
                    # parse_batch 内部已经把每条文档的抽取/落库异常都吞掉
                    # 并推进了各自的状态机，能漏到这里的是批级别的问题
                    # （比如批量预读查询本身失败），整个 chunk 未提交的
                    # 部分一起回滚，靠租约过期自然重试。
                    report.failed += len(chunk)
                    await _abort(session, "解析", f"{kind}#{chunk[0].id}..", exc)
                    continue
                for result in results:
                    report.succeeded += int(result.ok)
                    report.failed += int(not result.ok)
                for doc in chunk:
                    await _mark_done(doc)
                await session.commit()

        if not claimed.rows:
            break
    return report


async def run_verify_batch(
    session: AsyncSession,
    *,
    limit: int = 20,
    lease: timedelta = DEFAULT_LEASE,
    limiter: RateLimiter | None = None,
    write_batch: int = 100,
) -> BatchReport:
    """循环领取、校验，直到到点复查的资源被清空。

    `limit` 是每批领取多少条，不是这次总共处理多少条 —— 队列有多少到点复查的
    资源就处理多少，不设总量上限。`write_batch` 是攒够多少条处理完的资源再
    提交一次，见模块 docstring。`check_resource` 不像 `parse_document` 那样
    自己兜底异常，探测/落库出错会直接抛到这里，因此校验阶段撞上 `_abort` 的
    概率比解析阶段更高，一次探测异常会连带丢掉同一撮里还未提交的其它几条。
    """
    report = BatchReport()

    while True:
        claimed = await claim_resources(session, limit=limit, lease=lease)
        report.claimed += len(claimed)
        report.reclaimed += claimed.reclaimed
        report.abandoned += claimed.abandoned

        pending = 0
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
                await _mark_done(resource)
                pending += 1
            except Exception as exc:
                report.failed += 1
                await _abort(session, "校验", resource.id, exc)
                pending = 0
                continue
            if pending >= write_batch:
                await session.commit()
                pending = 0
        if pending:
            await session.commit()

        if not claimed.rows:
            break
    return report
