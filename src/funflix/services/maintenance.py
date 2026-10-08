"""数据维护：重建流水线数据、重新归类标签。

这些操作会**不可逆地改数据**，所以放在服务层而不是 CLI 命令体里 ——
只存在于 Typer 命令里的算法既测不了，也没法被接口或 worker 复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from farlog import getLogger
from sqlalchemy import Table, bindparam, delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from funflix.base.dbconflict import is_write_conflict, retry_on_write_conflict
from funflix.base.enums import CHECKABLE_PROVIDERS, CheckStatus, ParseStatus, Provider
from funflix.models import (
    Base,
    LinkCheck,
    Media,
    RawDocument,
    Resource,
    Source,
    Tag,
    TagKind,
    Work,
    media_resource,
    media_tag,
    utcnow,
)
from funflix.services.counters import refresh_counters_for_media
from funflix.services.text.linkscan import identify_provider, is_non_resource_url
from funflix.services.text.normalize import classify_tag
from funflix.services.verify.base import CheckOutcome
from funflix.services.verify.runner import _next_check_at

logger = getLogger("funflix")

#: 重建时保留的表。采集源是**配置**，不是采集回来的数据。
#:
#: `user` 同理 —— 登录账号是身份配置，不是流水线产物。它原先在清空清单里，
#: 而全量重建的生产库只有一个账号：清掉之后运维区就登不进去了，密码哈希是
#: 单向的，`db reset` 的报告里也救不回来，只能让人重新 `funflix user create`。
#: `db reset --help` 写的是「采集源配置保留」，顺手清掉账号属于意料之外的破坏。
PRESERVED_TABLES = frozenset({"source", "user", "alembic_version"})

#: `relink_checks` 每次往返携带的行数。见那里关于 84 万次往返的说明。
#:
#: 从 5000 降到 2000 是为了缩小跟并行节点撞死锁的窗口：一条 UPDATE 带多少行就
#: 同时持有多少把行锁，而 CI 里 parse 正在写同一批 `resource`。往返次数从
#: 170 次涨到 425 次，在这个量级上无所谓；真撞上了，一批的重试代价也小一半多。
_RELINK_BATCH = 2_000


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
    """`reset_pipeline_data` 的执行结果。

    Attributes:
        tables: 本次实际被清空（TRUNCATE/DELETE）的表名，按外键依赖倒序。
        before: 清空前的记录数，键为表名；覆盖全部数据表，包括本次未被清空的表
            （比如 `keep_documents=True` 时的 `raw_document`）。
        after: 清空并重建完成后的记录数，统计口径同 `before`。
        cursors_reset: 采集源水位是否被归零。
        checks_purged: `link_check` 校验历史是否被一并清空。
        documents_requeued: 因保留原始文本而被重新置回 `PENDING` 待解析状态的文档数。
    """

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
    """`relink_checks` 的执行结果：从历史校验记录恢复了校验状态的 resource 数。

    Attributes:
        hydrated: 成功恢复了校验状态的 resource 数。
        conflicted: 撞并发写入冲突、重试耗尽后放弃的行数（见 `relink_checks`）。
    """

    hydrated: int = 0
    conflicted: int = 0


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

    ## 为什么是攒批 executemany，而不是逐行查改

    这个函数原来对每条历史记录发一次 `session.scalar` 去找对应 resource。
    生产库有 848,416 个去重链接 —— 那就是 84 万次网络往返，全量重建卡在这一步
    要跑到天荒地老。现在改成：一条流式查询读出每个链接的最新结论，按
    `_RELINK_BATCH` 攒批，用带 `bindparam` 的 `UPDATE` 走 executemany，
    往返降到 170 次左右。

    `check_attempts` 恒为 0 这件事顺手把 `_next_check_at` 简化掉了：它只读
    `check_attempts` 和 `status`，而这里只碰 `UNCHECKED` 的行 —— 一条 resource
    只要被真实校验过，status 就不再是 `UNCHECKED`（`RATE_LIMITED` / `ERROR`
    也都是落库的状态值）。所以 attempts 必然还是默认的 0，`_next_check_at`
    退化成「只看 status」的纯函数，每种状态预算一次即可，不必逐行调。
    """
    # 预算每种状态的下次复查时间。`now` 在整个过程里会漂移几分钟，
    # 而 TTL 以天计，无关紧要。
    next_check_by_status = {
        status: _next_check_at(Resource(check_attempts=0), CheckOutcome(status=status))
        for status in CheckStatus
    }

    # **必须打到 Core 表上，不能用 ORM 实体。** `session.execute(update(Resource), [...])`
    # 会被 ORM 解释成「按主键批量更新」，于是要求每个字典都带 `resource.id` ——
    # 而我们正是因为不知道 id 才按 (provider, share_id) 去找。走 `__table__`
    # 就是一条普通的带 WHERE 的 UPDATE + executemany。
    #
    # 代价是身份映射不会同步：会话里已加载的 Resource 对象读到的还是旧值。
    # 这个函数跑在刚 `db reset` 完的库上，本来就没有这种对象；
    # 调用方（含单测）要看改后的值得自己 `session.refresh()`。
    #
    # bindparam 的名字刻意不叫 `status` / `provider` —— 和列名重名会跟
    # SET 子句自动生成的参数撞上。
    # `__table__` 的静态类型是宽泛的 `FromClause`，而 `update()` 要 `TableClause`；
    # 运行时它就是 `Table`，cast 只是把类型说准。
    table = cast(Table, Resource.__table__)
    stmt = (
        update(table)
        .where(
            table.c.provider == bindparam("p"),
            table.c.share_id == bindparam("s"),
            # 已经被真实校验过、或上一轮已恢复过的行不覆盖。
            table.c.check_status == CheckStatus.UNCHECKED,
        )
        .values(
            check_status=bindparam("st"),
            last_checked_at=bindparam("lc"),
            next_check_at=bindparam("nc"),
        )
    )

    unchecked_before = await _unchecked_count(session)

    # 「最新一条」按 `checked_at` 排，**不能用 `max(id)`**。id 是 uuid7，
    # 毫秒级单调 —— 同一毫秒内落库的两条校验，它们 id 的大小由随机位决定，
    # 于是 `max(id)` 会随机挑一条。一条链接从 valid 变成 invalid、两条记录又
    # 恰好同毫秒时，这个函数就会把早已失效的链接恢复成 valid。
    # id 留作同一时刻的稳定 tie-break。
    #
    # 窗口函数而不是 `DISTINCT ON`：后者只有 PG 有，单测跑在 SQLite 上。
    ranked = select(
        LinkCheck.provider,
        LinkCheck.share_id,
        LinkCheck.status,
        LinkCheck.checked_at,
        func.row_number()
        .over(
            partition_by=(LinkCheck.provider, LinkCheck.share_id),
            order_by=(LinkCheck.checked_at.desc(), LinkCheck.id.desc()),
        )
        .label("rn"),
    ).subquery()
    # 只取需要的四列：848k 行不值得实例化成 ORM 对象。
    latest = select(
        ranked.c.provider, ranked.c.share_id, ranked.c.status, ranked.c.checked_at
    ).where(ranked.c.rn == 1)
    rows = await session.stream(latest)

    conflicted = 0
    batch: list[dict[str, object]] = []
    async for provider, share_id, status, checked_at in rows:
        batch.append(
            {
                "p": provider,
                "s": share_id,
                "st": status,
                "lc": checked_at,
                "nc": next_check_by_status[status],
            }
        )
        if len(batch) >= _RELINK_BATCH:
            conflicted += await _flush_relink_batch(session, stmt, batch)
            batch.clear()
    if batch:
        conflicted += await _flush_relink_batch(session, stmt, batch)

    await session.commit()
    # 逐行数不出来（executemany 的 rowcount 不可靠），用前后差值 —— 这是精确的，
    # 因为本函数是把 resource 从 `UNCHECKED` 改走的唯一来源。
    return RelinkReport(
        hydrated=unchecked_before - await _unchecked_count(session), conflicted=conflicted
    )


async def _flush_relink_batch(
    session: AsyncSession, stmt: Update, batch: list[dict[str, object]]
) -> int:
    """落一批回填，撞车就重试；重试耗尽则放弃这批，返回放弃的行数。

    **一批失败不能把整个函数带走。** 这个函数做的是「省一遍重探」的优化：
    放弃的那些行留在 `UNCHECKED`，verify 会照常去探，结论一样，只是多花
    一次网络请求。而让异常冒出去的代价大得多 —— 实测是整个 verify job 在
    `Relink` 这一步退出 1，后面的 `funflix verify` 一条都没跑。

    开 SAVEPOINT 而不是直接 `execute`：死锁会把当前事务打进 aborted 状态，
    不回滚的话后面每一批都会报「current transaction is aborted」，等于一次
    撞车废掉整轮。回滚到 SAVEPOINT 就能把事务救回来，前面已经落好的批次
    也不受影响（它们在更外层的事务里，commit 在函数末尾）。
    """

    async def _once() -> None:
        async with session.begin_nested():
            await session.execute(stmt, batch)

    try:
        await retry_on_write_conflict(_once, what=f"relink 这批 {len(batch)} 行")
    except DBAPIError as err:
        if not is_write_conflict(err):
            raise
        logger.warning(f"relink 这批 {len(batch)} 行撞车重试耗尽，放弃（留给 verify 正常探测）")
        return len(batch)
    return 0


async def _unchecked_count(session: AsyncSession) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Resource)
            .where(Resource.check_status == CheckStatus.UNCHECKED)
        )
    ) or 0


@dataclass(slots=True)
class RetagReport:
    """`retag_all` 的执行结果。

    Attributes:
        total: 处理前库中标签总数。
        moved: 维度判定发生变化、目标维度下尚无同名标签，直接改写 `kind` 的标签数。
        merged: 维度变更后与目标维度下已有同名标签撞上、关联被迁移且自身被删除
            的标签数。
        recounted: 合并完成后 `media_count` 被重新计算并修正的标签行数。
    """

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


@dataclass(slots=True)
class CleanupResourcesReport:
    """`cleanup_resources` 的执行结果。

    Attributes:
        other_scanned: 扫描到的 `provider=OTHER` 资源数。
        ctfile_found: 其中被识别为城通网盘链接的资源数。
        ctfile_reclassified: 按 share_id 首次出现的那条，被改写为
            `provider=CTFILE` 的资源数。
        duplicates_merged: 同一 share_id 下除首次出现者外被判定为重复、
            关联迁移后删除的资源数。
        blacklisted_deleted: 被识别为非资源网页链接而删除的资源数。
        media_recounted: 因资源被合并或删除而重新计算计数器的作品数。
    """

    other_scanned: int = 0
    ctfile_found: int = 0
    ctfile_reclassified: int = 0
    duplicates_merged: int = 0
    blacklisted_deleted: int = 0
    media_recounted: int = 0


def _chunks[T](items: list[T], size: int = 500) -> list[list[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def cleanup_resources(session: AsyncSession) -> CleanupResourcesReport:
    """重分类城通链接，合并身份冲突，并删除明确不是资源的网页链接。"""
    report = CleanupResourcesReport()
    ctfile: dict[str, list] = {}
    blacklisted: list = []
    rows = await session.stream(
        select(Resource.id, Resource.url).where(Resource.provider == Provider.OTHER)
    )
    async for resource_id, url in rows:
        report.other_scanned += 1
        identified = identify_provider(url)
        if identified and identified[0] is Provider.CTFILE:
            ctfile.setdefault(identified[1], []).append(resource_id)
        elif is_non_resource_url(url):
            blacklisted.append(resource_id)

    report.ctfile_found = sum(map(len, ctfile.values()))
    existing_ctfile = dict(
        (
            await session.execute(
                select(Resource.share_id, Resource.id).where(Resource.provider == Provider.CTFILE)
            )
        ).all()
    )
    duplicate_to_target: dict = {}
    updates: list[dict] = []
    for share_id, resource_ids in ctfile.items():
        target_id = existing_ctfile.get(share_id)
        if target_id is None:
            target_id = resource_ids.pop(0)
            updates.append({"id": target_id, "provider": Provider.CTFILE, "share_id": share_id})
        duplicate_to_target.update({resource_id: target_id for resource_id in resource_ids})

    duplicate_ids = list(duplicate_to_target)
    affected_media_ids: set = set()
    if duplicate_ids:
        involved = set(duplicate_ids) | set(duplicate_to_target.values())
        resources = {
            resource.id: resource
            for resource in await session.scalars(select(Resource).where(Resource.id.in_(involved)))
        }
        for source_id, target_id in duplicate_to_target.items():
            source = resources[source_id]
            target = resources[target_id]
            target.seen_count += source.seen_count
            target.first_seen_at = min(target.first_seen_at, source.first_seen_at)
            target.last_seen_at = max(target.last_seen_at, source.last_seen_at)
            for field in (
                "passcode",
                "title_raw",
                "episode_info",
                "size_bytes",
                "sharer_id",
                "sharer_name",
                "sharer_avatar_url",
            ):
                if getattr(target, field) is None:
                    setattr(target, field, getattr(source, field))

        pairs = (
            await session.execute(
                select(
                    media_resource.c.media_id,
                    media_resource.c.resource_id,
                    media_resource.c.created_at,
                ).where(media_resource.c.resource_id.in_(involved))
            )
        ).all()
        existing_pairs = {(media_id, resource_id) for media_id, resource_id, _ in pairs}
        inserts = []
        for media_id, source_id, created_at in pairs:
            target_id = duplicate_to_target.get(source_id)
            if target_id is None or (media_id, target_id) in existing_pairs:
                continue
            inserts.append(
                {"media_id": media_id, "resource_id": target_id, "created_at": created_at}
            )
            existing_pairs.add((media_id, target_id))
        affected_media_ids.update(media_id for media_id, _, _ in pairs)
        if inserts:
            await session.execute(media_resource.insert(), inserts)
        await session.execute(
            delete(media_resource).where(media_resource.c.resource_id.in_(duplicate_ids))
        )
        await session.execute(delete(Resource).where(Resource.id.in_(duplicate_ids)))
        report.duplicates_merged = len(duplicate_ids)

    for chunk in _chunks(updates):
        await session.execute(update(Resource), chunk)
    report.ctfile_reclassified = len(updates)

    for chunk in _chunks(blacklisted):
        affected_media_ids.update(
            await session.scalars(
                select(media_resource.c.media_id).where(media_resource.c.resource_id.in_(chunk))
            )
        )
        await session.execute(delete(Resource).where(Resource.id.in_(chunk)))
    report.blacklisted_deleted = len(blacklisted)

    for chunk in _chunks(list(affected_media_ids)):
        report.media_recounted += await refresh_counters_for_media(session, chunk)
    await session.commit()
    return report


#: 空壳作品至少要「静置」这么久才删。纯粹是给跨进程的中间态留余量：建作品和
#: 挂 media 虽然在同一个事务里（见 `canon/apply.py::_ensure_work`），但一旦将来
#: 有哪条路径把两件事拆开提交，没有这个窗口就会在那个缝里把活作品删掉。
PRUNE_MIN_AGE = timedelta(hours=1)

#: 每批删多少行。跟 `_chunks` 的默认值一致，没有特别的道理 —— 够小不至于把
#: 一个大事务拖太久，够大不至于让往返次数变成瓶颈。
PRUNE_CHUNK = 500


@dataclass(slots=True)
class PruneWorksReport:
    """`prune_empty_works` 的执行结果。

    Attributes:
        deleted: 删掉的空壳作品数。
        remaining: 这一轮 `limit` 没排上的空壳数，下一轮接着删。
    """

    deleted: int = 0
    remaining: int = 0


def _empty_work_ids(cutoff: datetime):
    """选出「没有任何 media 指向」且已经静置够久的作品 id。

    判定走 `NOT EXISTS` 而不是 `work.season_count == 0`：那两个计数是冗余列，
    由 `services/counters.py` 事后重算，本身就可能过期（生产库实测
    `season_count == 0` 有 33,633 行，而真的没有 media 指向的是 36,223 行 ——
    差的 2,590 行正是计数还没刷到的）。删行这种不可逆操作不能建立在可能过期的
    冗余列上。
    """
    return select(Work.id).where(
        ~select(Media.id).where(Media.work_id == Work.id).exists(),
        Work.created_at < cutoff,
    )


async def prune_empty_works(session: AsyncSession, *, limit: int | None = None) -> PruneWorksReport:
    """删掉没有任何 media 指向的空壳作品。

    这些行是 rehome / merge 的残留：media 被搬到别的作品下或被合并掉之后，原
    作品就空了，但没人负责删它。`canon/rebuild.py` 里那句「那是 maintenance
    的活」说的就是这件事。

    为什么必须删：搜索默认**不**过滤空壳 —— `services/search.py::_apply_filters`
    只在 `valid_only` 时才要求「至少有一条校验通过的资源」，不带这个参数的列表页
    和关键词搜索会把空壳一起吐出去，用户点进去是空的。生产库实测 36,223 行、
    占作品总数 17.9%。

    删 work 是不可逆的，所以两道保险：一是只认 `NOT EXISTS`（见
    `_empty_work_ids`），二是只删静置超过 `PRUNE_MIN_AGE` 的行。`media.work_id`
    是 `ondelete="CASCADE"`，但这里删的恰恰是没有 media 的行，级联不会触发。

    Args:
        session: 数据库会话。
        limit: 这一轮最多删多少行；None 表示删到没有为止。分轮是为了让它能
            挂在有时间预算的 CI job 里 —— 首轮三万多行，一次删完的大事务
            会把 job 占满。

    Returns:
        删掉的行数，以及这一轮没排上、留给下一轮的行数。
    """
    report = PruneWorksReport()
    cutoff = utcnow() - PRUNE_MIN_AGE
    budget = limit

    while budget is None or budget > 0:
        size = PRUNE_CHUNK if budget is None else min(PRUNE_CHUNK, budget)
        victims = list(await session.scalars(_empty_work_ids(cutoff).limit(size)))
        if not victims:
            return report
        await session.execute(delete(Work).where(Work.id.in_(victims)))
        # 按批提交：中断时已经删掉的那些不会回滚，下一轮从剩下的接着来。
        await session.commit()
        report.deleted += len(victims)
        if budget is not None:
            budget -= len(victims)

    report.remaining = (
        await session.scalar(select(func.count()).select_from(_empty_work_ids(cutoff).subquery()))
        or 0
    )
    return report


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
