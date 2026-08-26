"""任务领取：把「状态列 + 租约」当成队列表。见 docs/DESIGN.md §5。

领取的核心是一条带守卫的 UPDATE：

    UPDATE raw_document SET parse_status='running', lease_until=:until
     WHERE id=:id AND <与候选查询完全相同的条件>

`rowcount == 1` 才算领到。两个 worker 同时盯上同一行时，只有一个的 UPDATE
能命中，另一个拿到 0 行、自动跳过 —— 不需要额外的锁表，也不依赖方言特性。
守卫条件与候选查询共用同一个函数，就是为了保证两者不会各自演化到对不上。

**为什么不用 `SELECT ... FOR UPDATE SKIP LOCKED`**：SQLite 不支持，而
schema 必须同时跑在 SQLite 与 PostgreSQL 上（§1）。

**为什么逐行领而不是一条 UPDATE 批量领**：批量领分辨不出每一行是「新任务」
还是「上一个 worker 崩溃后被重捞的任务」。后者必须计入重试次数，否则一条
会让进程崩溃的毒任务会被无限重捞 —— worker 起来、崩掉、再起来，永远卡在它上面。
批次很小（默认 20），而每条任务后面跟着的是一次 LLM 调用或一次网络探测，
比多出来的这几次 UPDATE 慢好几个数量级，省不出什么。

**过期租约即补偿**：候选条件同时接受「pending 且无租约」与「running 且租约已过期」，
所以崩溃的任务在租约到期后会自动回到队列，不需要单独的启动补偿扫描。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.backoff import backoff
from funflix.base.enums import CHECKABLE_PROVIDERS, CheckStatus, ParseStatus
from funflix.models import RawDocument, Resource, Source, utcnow
from funflix.services.extract.runner import MAX_PARSE_ATTEMPTS

logger = logging.getLogger(__name__)

#: 租约时长。worker 崩溃后，任务最多被卡这么久就会被重新领取。
#: 必须显著长于单条任务的正常耗时（LLM 调用可能几十秒），否则任务还在跑
#: 租约就过期了，会被另一个 worker 重复领取，白烧一次 token。
DEFAULT_LEASE = timedelta(minutes=5)


@dataclass(slots=True)
class Claimed[T]:
    """一次领取的结果。"""

    rows: list[T] = field(default_factory=list)
    #: 从过期租约里重捞回来的数量。持续大于 0 意味着有 worker 在反复崩溃。
    reclaimed: int = 0
    #: 重捞时发现已超过重试上限、直接置终态的数量。
    abandoned: int = 0
    #: 主键 → 领取前的状态（即 `columns` 的第二列，按约定就是状态列）。
    #:
    #: 领取会把状态改写成 running / checking 这类"正在处理"的占位值，
    #: 而下游需要知道领取前的真实结论 —— 比如 `check_resource` 要拿它判断
    #: "这次的失效结论是不是上一次的延续"。占位值比不出连续性。
    priors: dict[Any, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)


def _overfetch(limit: int) -> int:
    """多捞一些候选，抵消被别的 worker 抢走的部分。"""
    return limit * 2 + 8


async def _claim_rows[T](
    session: AsyncSession,
    model: type[T],
    *,
    columns: list[Any],
    conditions: list[ColumnElement[bool]],
    order_by: list[Any],
    limit: int,
    decide: Callable[..., tuple[dict[str, Any], bool, bool]],
) -> Claimed[T]:
    """领取的通用骨架。

    Args:
        columns: 候选查询要取的列，第一列必须是主键，其余传给 `decide`。
        conditions: 候选条件，同时用作 UPDATE 的守卫。
        decide: `(*rest_columns) -> (values, is_reclaim, is_terminal)`。
            `values` 是要写入的列，`is_terminal` 为真表示这行被置了终态、
            不算领到手的任务。
    """
    result: Claimed[T] = Claimed()
    if limit <= 0:
        return result

    candidates = (
        await session.execute(
            select(*columns).where(*conditions).order_by(*order_by).limit(_overfetch(limit))
        )
    ).all()

    claimed_ids: list[Any] = []
    for row_id, *rest in candidates:
        if len(claimed_ids) >= limit:
            break

        values, is_reclaim, is_terminal = decide(*rest)
        updated = await session.execute(
            update(model).where(model.id == row_id, *conditions).values(**values)  # type: ignore[attr-defined]
        )
        if updated.rowcount != 1:
            # 在我们读到候选和写入之间，别的 worker 抢先领走了它。正常竞争，跳过。
            continue

        if is_terminal:
            result.abandoned += 1
            continue
        result.reclaimed += int(is_reclaim)
        result.priors[row_id] = rest[0] if rest else None
        claimed_ids.append(row_id)

    await session.commit()

    if claimed_ids:
        result.rows = list(
            await session.scalars(
                select(model).where(model.id.in_(claimed_ids)).order_by(model.id)  # type: ignore[attr-defined]
            )
        )
    return result


async def claim_documents(
    session: AsyncSession,
    *,
    limit: int = 20,
    lease: timedelta = DEFAULT_LEASE,
    now: datetime | None = None,
) -> Claimed[RawDocument]:
    """领取待解析的原始文本，置 running 并加租约。"""
    now = now or utcnow()
    until = now + lease
    conditions = [
        # running 也在候选里：那是上一个 worker 崩在半路留下的，
        # 配合下一行的"租约已过期"，它就是被重捞的对象。
        RawDocument.parse_status.in_((ParseStatus.PENDING, ParseStatus.RUNNING)),
        or_(RawDocument.lease_until.is_(None), RawDocument.lease_until <= now),
        or_(RawDocument.next_parse_at.is_(None), RawDocument.next_parse_at <= now),
    ]

    def decide(status: ParseStatus, attempts: int) -> tuple[dict[str, Any], bool, bool]:
        is_reclaim = status is ParseStatus.RUNNING
        if is_reclaim and attempts + 1 >= MAX_PARSE_ATTEMPTS:
            # 已经崩够次数了。这类文档往往能稳定复现（超长正文、畸形编码），
            # 继续重捞只会让 worker 反复自杀，把整个队列堵死。
            return (
                {
                    "parse_status": ParseStatus.FAILED,
                    "parse_attempts": attempts + 1,
                    "parse_error": "worker 处理该文档时中断，且重试次数已达上限",
                    "lease_until": None,
                    "next_parse_at": None,
                },
                True,
                True,
            )
        return (
            {
                "parse_status": ParseStatus.RUNNING,
                # 只有重捞才计数。正常失败由 parse_document 自己累加，
                # 在这里再加一次会让重试次数翻倍消耗。
                "parse_attempts": attempts + 1 if is_reclaim else attempts,
                "lease_until": until,
            },
            is_reclaim,
            False,
        )

    claimed = await _claim_rows(
        session,
        RawDocument,
        columns=[RawDocument.id, RawDocument.parse_status, RawDocument.parse_attempts],
        conditions=conditions,
        order_by=[RawDocument.id],
        limit=limit,
        decide=decide,
    )
    if claimed.reclaimed or claimed.abandoned:
        logger.warning(
            "解析队列重捞 %s 条过期任务，其中 %s 条已超重试上限置为 failed",
            claimed.reclaimed,
            claimed.abandoned,
        )
    return claimed


async def claim_resources(
    session: AsyncSession,
    *,
    limit: int = 20,
    lease: timedelta = DEFAULT_LEASE,
    now: datetime | None = None,
) -> Claimed[Resource]:
    """领取待校验的资源，置 checking 并加租约。

    只领 `CHECKABLE_PROVIDERS` 里的 provider —— 其余的在落库时就已经是
    `unsupported`，没有探针可用，领了也只能原样放回。
    """
    now = now or utcnow()
    until = now + lease
    conditions = [
        Resource.provider.in_(CHECKABLE_PROVIDERS),
        or_(Resource.lease_until.is_(None), Resource.lease_until <= now),
        or_(
            Resource.next_check_at <= now,
            # 刚落库、还没排过复查时间的
            Resource.next_check_at.is_(None) & (Resource.check_status == CheckStatus.UNCHECKED),
        ),
    ]

    def decide(status: CheckStatus, attempts: int) -> tuple[dict[str, Any], bool, bool]:
        is_reclaim = status is CheckStatus.CHECKING
        values: dict[str, Any] = {"check_status": CheckStatus.CHECKING, "lease_until": until}
        if is_reclaim:
            # 探测本身很便宜，不像解析那样需要终态；靠退避把重捞频率压下去即可，
            # 退避封顶 6 小时，不会变成热循环。
            values["check_attempts"] = attempts + 1
            values["next_check_at"] = now + backoff(attempts + 1)
        return values, is_reclaim, False

    claimed = await _claim_rows(
        session,
        Resource,
        columns=[Resource.id, Resource.check_status, Resource.check_attempts],
        conditions=conditions,
        # 没校验过的排在最前：新资源的第一次结论比老资源的复查更有价值
        order_by=[Resource.next_check_at.nulls_first(), Resource.id],
        limit=limit,
        decide=decide,
    )
    if claimed.reclaimed:
        logger.warning("校验队列重捞 %s 条过期任务", claimed.reclaimed)
    return claimed


async def claim_sources(
    session: AsyncSession,
    *,
    limit: int = 5,
    lease: timedelta = DEFAULT_LEASE,
    now: datetime | None = None,
) -> Claimed[Source]:
    """领取到点该采集的源。

    Source 没有状态列，租约本身就是互斥凭据 —— 同一个源被两个 worker 同时采，
    会各自推进一次水位，中间的消息就被跳过了。
    """
    now = now or utcnow()
    until = now + lease
    conditions = [
        Source.enabled.is_(True),
        or_(Source.lease_until.is_(None), Source.lease_until <= now),
        or_(Source.next_fetch_at.is_(None), Source.next_fetch_at <= now),
    ]

    def decide(lease_until: datetime | None, failures: int) -> tuple[dict[str, Any], bool, bool]:
        # 有过租约又落到候选里，只可能是上一轮没能正常收尾。
        is_reclaim = lease_until is not None
        values: dict[str, Any] = {"lease_until": until}
        if is_reclaim:
            # 复用 consecutive_failures 这套健康度机制：崩溃跟网络失败一样，
            # 都该让这个源退避，而不是每个周期都去踩同一个坑。
            values["consecutive_failures"] = failures + 1
            values["next_fetch_at"] = now + backoff(failures + 1)
        return values, is_reclaim, False

    claimed = await _claim_rows(
        session,
        Source,
        columns=[Source.id, Source.lease_until, Source.consecutive_failures],
        conditions=conditions,
        order_by=[Source.next_fetch_at.nulls_first(), Source.id],
        limit=limit,
        decide=decide,
    )
    if claimed.reclaimed:
        logger.warning("采集队列重捞 %s 个中断的源", claimed.reclaimed)
    return claimed
