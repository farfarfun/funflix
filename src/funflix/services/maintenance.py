"""数据维护：重建流水线数据、重新归类标签。

这些操作会**不可逆地改数据**，所以放在服务层而不是 CLI 命令体里 ——
只存在于 Typer 命令里的算法既测不了，也没法被接口或 worker 复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import CHECKABLE_PROVIDERS, CheckStatus, ParseStatus
from funflix.models import (
    Base,
    LinkCheck,
    RawDocument,
    Resource,
    Source,
    Tag,
    TagKind,
    media_tag,
    utcnow,
)
from funflix.services.text.normalize import classify_tag
from funflix.services.verify.base import CheckOutcome
from funflix.services.verify.runner import _next_check_at

#: 重建时保留的表。采集源是**配置**，不是采集回来的数据。
PRESERVED_TABLES = frozenset({"source", "alembic_version"})


def data_tables(keep_documents: bool = False, purge_checks: bool = False) -> list[str]:
    """列出重建时要清空的表，按外键依赖倒序（先删子表）。

    从 ORM 元数据推导，不手工维护清单。曾经这里是一个写死的六元组，
    后来加的 `tag` / `media_tag` 没人记得补进去 —— 结果 `db reset` 之后
    `tag` 行还在、`media_count` 还停在旧值，而 `media` 已经空了；
    重新解析时这些标签被复用，计数从错误的基数上继续累加，一次比一次离谱。

    表是数据库结构的一部分，让结构自己说清楚有哪些表，比让人记得同步一份
    副本可靠得多。

    `link_check` 默认也排除在外：它跟 `resource` 没有外键，完全独立存储，只按
    (provider, share_id) 锚定身份（见 models/check.py），`resource` 被清空重建
    不会碰到它。校验历史是全库成本最高的数据（每条都要真实探测网盘接口），
    默认不跟着 resource 陪葬；真要连它一起清（比如联调建库），传 `purge_checks=True`。
    """
    names = []
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in PRESERVED_TABLES:
            continue
        if keep_documents and table.name == "raw_document":
            continue
        if not purge_checks and table.name == "link_check":
            continue
        names.append(table.name)
    return names


@dataclass(slots=True)
class ResetReport:
    tables: list[str] = field(default_factory=list)
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    cursors_reset: bool = False
    checks_purged: bool = False
    documents_requeued: int = 0


async def _counts(session: AsyncSession, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in tables:
        out[table] = (await session.execute(text(f"select count(*) from {table}"))).scalar() or 0
    return out


async def reset_pipeline_data(
    session: AsyncSession,
    *,
    keep_documents: bool = False,
    keep_cursors: bool = False,
    purge_checks: bool = False,
) -> ResetReport:
    """清空流水线数据，保留采集源配置。

    Args:
        keep_documents: 保留原始文本，只重建下游解析结果。
        keep_cursors: 保留采集水位。清空原始文本时**不要**用 ——
            水位还在的话采集器会认为"都采过了"，重建后一条也拉不回来。
        purge_checks: 连校验历史（`link_check`）一起清空。默认不清 ——
            见 `data_tables` 里的说明；重解析出新 resource 后可以用
            `relink_checks` 把历史接回来。
    """
    tables = data_tables(keep_documents=keep_documents, purge_checks=purge_checks)
    if keep_documents and not keep_cursors:
        # 原始文本还在，水位归零只会导致重复采集后被 content_hash 挡掉，无意义
        keep_cursors = True

    # 报告覆盖全部数据表，而不只是这次被清空的那些 —— 用了 --keep-documents
    # 的人最想确认的恰恰是"原始文本还在不在"，只报清空的表就看不到它。
    reported = [*data_tables(purge_checks=True), "source"]
    report = ResetReport(tables=tables, before=await _counts(session, reported))

    if session.bind.dialect.name == "postgresql":
        await session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    else:
        # SQLite 没有 TRUNCATE，逐表 DELETE。tables 已按依赖倒序，先删子表。
        for table in tables:
            await session.execute(text(f"DELETE FROM {table}"))

    if not keep_cursors:
        for source in await session.scalars(select(Source)):
            source.reset_watermark()

    if keep_documents:
        # `raw_document` 本身没被清空，但它身上的解析任务状态机字段记录的是
        # "旧一轮 parse 跑到哪了"——下游 resource/extraction 已经被清空重建，
        # 这些字段如果不跟着重置，`done`/`skipped`/`failed` 状态的文档会被
        # 领取查询（`ix_raw_document_parse_queue` 只认 `parse_status == PENDING`）
        # 永久跳过，绝大多数文档就再也不会被重新解析。
        result = await session.execute(
            update(RawDocument).values(
                parse_status=ParseStatus.PENDING,
                parse_attempts=0,
                parse_error=None,
                lease_until=None,
                next_parse_at=None,
                last_parsed_at=None,
            )
        )
        report.documents_requeued = result.rowcount or 0

    await session.commit()
    report.after = await _counts(session, reported)
    report.cursors_reset = not keep_cursors
    report.checks_purged = purge_checks
    return report


@dataclass(slots=True)
class RelinkReport:
    hydrated: int = 0


async def relink_checks(session: AsyncSession) -> RelinkReport:
    """用已有的校验历史恢复重新解析后新建的 resource 的校验状态。

    `link_check` 跟 `resource` 没有外键，`resource` 被 `reset_pipeline_data`
    清空重建完全不影响它。重新 parse 会按 (provider, share_id) 幂等 upsert 出
    同样身份的新 resource，但这些新 resource 的 `check_status` 是默认值
    `UNCHECKED`——这里按 (provider, share_id) 找回每条链接最新一条历史，把
    `check_status`/`last_checked_at`/`next_check_at` 恢复回去，这样重解析之后
    不用把全部资源重新探测一遍。

    `check_attempts` 不做精确复原（新 resource 保持默认值 0）——精确复原要扫完整
    历史计数，多余；副作用最多是极少数刚确认失效两次的链接会多等一轮 TTL 才停止
    复查，不影响正确性。
    """
    latest_check_ids = select(func.max(LinkCheck.id)).group_by(
        LinkCheck.provider, LinkCheck.share_id
    )
    hydrated = 0
    for check in await session.scalars(select(LinkCheck).where(LinkCheck.id.in_(latest_check_ids))):
        resource = await session.scalar(
            select(Resource).where(
                Resource.provider == check.provider, Resource.share_id == check.share_id
            )
        )
        if resource is None or resource.check_status is not CheckStatus.UNCHECKED:
            # 没有对应的新 resource，或者已经不是刚重建出来的默认状态
            # （已被真实校验过或已恢复），不覆盖。
            continue
        resource.check_status = check.status
        resource.last_checked_at = check.checked_at
        resource.next_check_at = _next_check_at(resource, CheckOutcome(status=check.status))
        hydrated += 1

    await session.commit()
    return RelinkReport(hydrated=hydrated)


@dataclass(slots=True)
class RetagReport:
    total: int = 0
    moved: int = 0
    merged: int = 0
    recounted: int = 0


async def retag_all(session: AsyncSession) -> RetagReport:
    """按当前规则重新归类已有标签。

    维度判定规则会迭代（比如题材白名单），但规则只影响**新建**的标签 ——
    改规则前存进去的行不会自己变。这里把历史数据补齐。

    同一个标签名在新旧维度下各有一行时会合并：关联迁到新行，旧行删除。
    """
    report = RetagReport()
    tags = list(await session.scalars(select(Tag)))
    report.total = len(tags)
    # 先建索引，避免每次都查库
    by_identity = {(t.kind.value, t.norm_key): t for t in tags}

    for tag in tags:
        new_kind = classify_tag(tag.name)
        if new_kind == tag.kind.value:
            continue

        target = by_identity.get((new_kind, tag.norm_key))
        if target is None or target.id == tag.id:
            tag.kind = TagKind(new_kind)
            by_identity[(new_kind, tag.norm_key)] = tag
            report.moved += 1
            continue

        # 目标维度下已有同名标签：把关联迁过去再删旧行。
        # 迁移前要剔掉两边都有的作品，否则会撞 (media_id, tag_id) 唯一键。
        dupes = select(media_tag.c.media_id).where(media_tag.c.tag_id == target.id)
        await session.execute(
            update(media_tag)
            .where(media_tag.c.tag_id == tag.id, media_tag.c.media_id.not_in(dupes))
            .values(tag_id=target.id)
        )
        await session.execute(delete(media_tag).where(media_tag.c.tag_id == tag.id))
        await session.delete(tag)
        report.merged += 1

    await session.flush()
    report.recounted = await recount_tags(session)
    await session.commit()
    return report


async def requeue_now_checkable(session: AsyncSession) -> int:
    """把「新支持的网盘」的历史资源放回校验队列。返回被重新排队的条数。

    落库时不在 `CHECKABLE_PROVIDERS` 里的 provider 会被写成
    `unsupported` + `next_check_at=NULL`，而领取条件要求 `next_check_at` 到期，
    所以这些行**永远不会被领取**。

    于是新增一个探针（比如 UC）之后，库里已有的那批链接会静默地一直不被校验 ——
    新链接正常校验、老链接永远停在 unsupported，很难注意到。
    加完探针记得跑一次这个，或者 `funflix db requeue`。
    """
    result = await session.execute(
        update(Resource)
        .where(
            Resource.check_status == CheckStatus.UNSUPPORTED,
            Resource.provider.in_(CHECKABLE_PROVIDERS),
        )
        .values(check_status=CheckStatus.UNCHECKED, next_check_at=utcnow())
    )
    await session.commit()
    return result.rowcount or 0


async def recount_tags(session: AsyncSession) -> int:
    """按关联表重算全部标签的 `media_count`。返回被修正的行数。

    与 `services.counters` 同一个取舍：重算而非增量维护。
    """
    counts = dict(
        (
            await session.execute(
                select(media_tag.c.tag_id, func.count()).group_by(media_tag.c.tag_id)
            )
        ).all()
    )
    fixed = 0
    for tag in await session.scalars(select(Tag)):
        actual = counts.get(tag.id, 0)
        if tag.media_count != actual:
            tag.media_count = actual
            fixed += 1
    return fixed
