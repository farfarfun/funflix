"""解析流水线：抽取 → 归一 → 落库。

对外两个入口：`parse_document` 处理单条，`parse_batch` 处理一批（同一
extractor）。二者共用同一套幂等 upsert / 状态机推进 / 失败退避逻辑，区别
只在于要不要用 `BatchCache` 把 media/resource/tag 的去重查询从"每条文档
一次往返"折叠成"每批几次往返"——单条 SELECT 在远程数据库上一次就要
~100~150ms，一条文档要查好几次（缓存命中、media 去重、resource 去重、
tag 去重、关联是否已存在……），批量预读把这些查询从 O(条数) 降到 O(1)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.backoff import backoff
from funflix.base.enums import CHECKABLE_PROVIDERS, CheckStatus, MediaType, ParseStatus, Quality
from funflix.models import (
    Extraction,
    Media,
    RawDocument,
    Resource,
    Tag,
    TagKind,
    media_resource,
    media_tag,
    utcnow,
)
from funflix.models.media import UNKNOWN_YEAR
from funflix.services.counters import refresh_media_counters
from funflix.services.extract.base import ExtractedItem, ExtractionOutcome, Extractor
from funflix.services.text.linkscan import ScannedLink
from funflix.services.text.normalize import tag_norm_key

logger = logging.getLogger(__name__)

#: 连续失败这么多次后置终态 failed，不再自动重试。
#: worker 领取时也要用它判断"崩溃重捞"是否已经捞够次数，故为公开常量。
MAX_PARSE_ATTEMPTS = 5

#: `persist_extracted` 里共享一个 SAVEPOINT 的文档数上限。一条文档落库要
#: 固定几次往返（SAVEPOINT begin/release + 每张涉及表各一次 INSERT/UPDATE），
#: 在远程数据库上这个固定开销才是主要耗时，不是链接/标签数量。把 N 条文档
#: 打包共享一个 SAVEPOINT、一次 flush，往返次数就从 O(文档数) 摊薄成
#: O(文档数 / N)。代价是失败隔离变粗：一条文档撞唯一约束会连累同批其余
#: 文档一起回滚重试（不计入它们的失败次数，见 `persist_extracted`）。
SAVEPOINT_BATCH_SIZE = 20


@dataclass(slots=True)
class ParseReport:
    document_id: int
    status: ParseStatus
    is_catalog: bool = False
    from_cache: bool = False
    media_created: int = 0
    media_reused: int = 0
    resources_created: int = 0
    resources_updated: int = 0
    #: 新建的「作品↔资源」关联数。一个链接关联多部作品时会大于资源数。
    links_created: int = 0
    #: 新建的「作品↔标签」关联数
    tags_linked: int = 0
    unattributed_links: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class BatchCache:
    """一批文档共用的去重键缓存。

    只缓存"这一批已经查到/新建过"的行——不是全局缓存，每次 `parse_batch`
    调用都是一份新的。命中就跳过 SELECT，未命中仍然退化成单条查询/插入，
    正确性与不传 cache 时完全一致，只是把重复查询摊掉。
    """

    media_by_key: dict[tuple[str, MediaType, int], Media] = field(default_factory=dict)
    #: (norm_key, year) → 候选列表，用于类型放宽匹配
    media_by_relaxed: dict[tuple[str, int], list[Media]] = field(default_factory=dict)
    resource_by_key: dict[tuple[str, str], Resource] = field(default_factory=dict)
    tag_by_key: dict[tuple[str, str], Tag] = field(default_factory=dict)
    media_resource_pairs: set[tuple[int, int]] = field(default_factory=set)
    media_tag_pairs: set[tuple[int, int]] = field(default_factory=set)


def keyset_after(ts_col: Any, id_col: Any, last_ts: Any, last_id: int) -> Any:
    """`ORDER BY ts_col.nulls_first(), id_col` 场景下的翻页游标条件。

    不能直接拿 `id_col > last_id` 当游标——排序主键换成了 ts_col 之后，
    id 不再单调对应排序位置，会跳过或重复行。这里按 (ts_col, id_col)
    复合键翻页，NULL 视为最小值，跟 NULLS FIRST 的排序语义对齐。

    `cli.py` 的 `parse`/`verify` 命令与 `concurrent_runner.py` 的生产者
    共用同一份翻页逻辑。
    """
    if last_ts is None:
        # 上一页最后一行还在"从未处理过"（ts IS NULL）这一段里：后续行
        # 要么还在这段里但 id 更大，要么已经进入非 NULL 段（任意值都排后面）。
        return or_(and_(ts_col.is_(None), id_col > last_id), ts_col.is_not(None))
    return or_(ts_col > last_ts, and_(ts_col == last_ts, id_col > last_id))


async def _load_cached(
    session: AsyncSession, doc_id: int, name: str, version: str
) -> Extraction | None:
    """按 (文档, 抽取器身份, 版本) 找留档。换抽取器或升版本都会 miss，从而重新抽取。"""
    return await session.scalar(
        select(Extraction).where(
            Extraction.raw_document_id == doc_id,
            Extraction.model == name,
            Extraction.prompt_version == version,
        )
    )


async def _load_cached_batch(
    session: AsyncSession, doc_ids: list[int], name: str, version: str
) -> dict[int, Extraction]:
    """`_load_cached` 的批量版本：一次查询整批文档的留档。"""
    if not doc_ids:
        return {}
    rows = list(
        await session.scalars(
            select(Extraction).where(
                Extraction.raw_document_id.in_(doc_ids),
                Extraction.model == name,
                Extraction.prompt_version == version,
            )
        )
    )
    return {row.raw_document_id: row for row in rows}


async def _preload_batch_cache(
    session: AsyncSession, outcomes: list[ExtractionOutcome]
) -> BatchCache:
    """按整批抽取产出用到的去重键，各发一次 IN 查询，填出 `BatchCache`。

    media/resource/tag 各一次查询，外加"这些已存在的 media 都关联了哪些
    resource/tag"再各一次——五次往返覆盖整批（可能几百条文档），
    而不是每条文档各查一遍。
    """
    cache = BatchCache()

    norm_years: set[tuple[str, int]] = set()
    provider_shares: set[tuple[str, str]] = set()
    tag_keys: set[tuple[str, str]] = set()

    for outcome in outcomes:
        for item in outcome.items:
            year = item.year if item.year is not None else UNKNOWN_YEAR
            norm_years.add((item.norm_key, year))
            for kind, name in item.tags:
                key = tag_norm_key(name)
                if key:
                    tag_keys.add((kind, key))
            for link in item.links:
                provider_shares.add((link.provider, link.share_id))
        for link in outcome.unattributed_links:
            provider_shares.add((link.provider, link.share_id))

    if norm_years:
        rows = await session.scalars(
            select(Media).where(tuple_(Media.norm_key, Media.year).in_(norm_years))
        )
        for media in rows:
            cache.media_by_key[(media.norm_key, media.media_type, media.year)] = media
            cache.media_by_relaxed.setdefault((media.norm_key, media.year), []).append(media)

    if provider_shares:
        rows = await session.scalars(
            select(Resource).where(
                tuple_(Resource.provider, Resource.share_id).in_(provider_shares)
            )
        )
        for resource in rows:
            cache.resource_by_key[(resource.provider, resource.share_id)] = resource

    if tag_keys:
        rows = await session.scalars(
            select(Tag).where(tuple_(Tag.kind, Tag.norm_key).in_(tag_keys))
        )
        for tag in rows:
            cache.tag_by_key[(tag.kind, tag.norm_key)] = tag

    media_ids = [m.id for m in cache.media_by_key.values()]
    if media_ids:
        pair_rows = await session.execute(
            select(media_resource.c.media_id, media_resource.c.resource_id).where(
                media_resource.c.media_id.in_(media_ids)
            )
        )
        cache.media_resource_pairs.update((mid, rid) for mid, rid in pair_rows)

        tag_pair_rows = await session.execute(
            select(media_tag.c.media_id, media_tag.c.tag_id).where(
                media_tag.c.media_id.in_(media_ids)
            )
        )
        cache.media_tag_pairs.update((mid, tid) for mid, tid in tag_pair_rows)

    return cache


async def _upsert_media(
    session: AsyncSession, item: ExtractedItem, cache: BatchCache | None = None
) -> tuple[Media, bool]:
    """按 (norm_key, media_type, year) 找已有作品，找不到才新建。

    多了一步「类型放宽」的回退：同一部作品在不同分享里可能一次被判成 tv、
    一次判成 unknown。若严格按三元组匹配，它们会变成两部作品。
    所以先精确匹配，再按 (norm_key, year) 放宽，并顺手把 unknown 升级成已知类型。

    传了 `cache` 时优先查内存、未命中才落到 SELECT——批内新建的 media 也会
    写回 cache，同批后续文档引用同一部作品不会再查一次库。
    """
    year = item.year if item.year is not None else UNKNOWN_YEAR
    exact_key = (item.norm_key, item.media_type, year)
    relaxed_key = (item.norm_key, year)

    if cache is not None:
        media = cache.media_by_key.get(exact_key)
        if media is None:
            for candidate in cache.media_by_relaxed.get(relaxed_key, []):
                if (
                    candidate.media_type is MediaType.UNKNOWN
                    and item.media_type is not MediaType.UNKNOWN
                ):
                    candidate.media_type = item.media_type
                    media = candidate
                    cache.media_by_key[exact_key] = media
                    break
                if item.media_type is MediaType.UNKNOWN:
                    media = candidate
                    break
    else:
        media = await session.scalar(
            select(Media).where(
                Media.norm_key == item.norm_key,
                Media.media_type == item.media_type,
                Media.year == year,
            )
        )
        if media is None:
            candidates = list(
                await session.scalars(
                    select(Media).where(Media.norm_key == item.norm_key, Media.year == year)
                )
            )
            for candidate in candidates:
                if (
                    candidate.media_type is MediaType.UNKNOWN
                    and item.media_type is not MediaType.UNKNOWN
                ):
                    candidate.media_type = item.media_type  # 用更确定的类型升级旧记录
                    media = candidate
                    break
                if item.media_type is MediaType.UNKNOWN:
                    media = candidate  # 本次判不出类型，沿用已有的
                    break

    if media is not None:
        if item.title not in media.aliases and item.title != media.title:
            media.aliases = [*media.aliases, item.title]
        if media.original_title is None and item.original_title:
            media.original_title = item.original_title
        return media, False

    media = Media(
        title=item.title,
        norm_key=item.norm_key,
        original_title=item.original_title,
        media_type=item.media_type,
        year=year,
        aliases=[],
    )
    session.add(media)
    # 不在这里单独 flush——新建的 media 先攒着，跟同一条文档里新建的
    # resource、Extraction 一起，由调用方（`_persist`）一次性 flush，
    # 把「一条文档有 N 个链接就要 N 次数据库往返」降到固定次数。
    if cache is not None:
        cache.media_by_key[exact_key] = media
        cache.media_by_relaxed.setdefault(relaxed_key, []).append(media)
    return media, True


async def _upsert_resource(
    session: AsyncSession,
    link: ScannedLink,
    *,
    doc: RawDocument,
    item: ExtractedItem | None,
    cache: BatchCache | None = None,
) -> tuple[Resource, bool]:
    """按 (provider, share_id) 幂等落库。返回 (资源, 是否新建)。"""
    now = utcnow()
    key = (link.provider, link.share_id)
    existing = (
        cache.resource_by_key.get(key)
        if cache is not None
        else await session.scalar(
            select(Resource).where(
                Resource.provider == link.provider, Resource.share_id == link.share_id
            )
        )
    )

    if existing is not None:
        # 同一份分享被多处转发 —— 记热度，不重复建行
        existing.last_seen_at = now
        existing.seen_count += 1
        if existing.passcode is None and link.passcode:
            existing.passcode = link.passcode
        if existing.title_raw is None and item is not None:
            existing.title_raw = item.title
        return existing, False

    checkable = link.provider in CHECKABLE_PROVIDERS
    resource = Resource(
        raw_document_id=doc.id,
        provider=link.provider,
        share_id=link.share_id,
        url=link.url,
        passcode=link.passcode,
        title_raw=item.title if item else None,
        # quality 非空列，未归属的链接要给默认值而不是 None
        quality=item.quality if item else Quality.UNKNOWN,
        episode_info=item.episode_info if item else None,
        check_status=CheckStatus.UNCHECKED if checkable else CheckStatus.UNSUPPORTED,
        next_check_at=now if checkable else None,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(resource)
    # 同 `_upsert_media`：不在这里单独 flush，攒到调用方一次性 flush。
    if cache is not None:
        cache.resource_by_key[key] = resource
    return resource, True


async def _link_media_resource(
    session: AsyncSession,
    media: Media,
    resource: Resource,
    cache: BatchCache | None = None,
    pending: list[dict] | None = None,
) -> bool:
    """建立作品 ↔ 资源关联。已存在则跳过。返回 True 表示新建了关联。

    先查后插而不是靠数据库的 ON CONFLICT —— 后者语法各方言不同，
    而 schema 要同时跑在 SQLite 和 PostgreSQL 上。

    新建的关联行默认不在这里单独 `execute` —— 传了 `pending` 就攒进去，
    由调用方（`_persist`）处理完整条文档后一次性批量 INSERT。一条文档
    可能有好几个链接，逐条发 INSERT 会让关联表写入的往返次数跟链接数
    成正比。
    """
    pair = (media.id, resource.id)
    if cache is not None:
        exists = pair in cache.media_resource_pairs
    else:
        exists = (
            await session.scalar(
                select(media_resource.c.media_id).where(
                    media_resource.c.media_id == media.id,
                    media_resource.c.resource_id == resource.id,
                )
            )
        ) is not None
    if exists:
        return False

    row = {"media_id": media.id, "resource_id": resource.id, "created_at": utcnow()}
    if pending is not None:
        pending.append(row)
    else:
        await session.execute(media_resource.insert().values(**row))
    if cache is not None:
        cache.media_resource_pairs.add(pair)
    # 计数不在这里 +1：调用方收尾时按关联表重算一次。
    # 自增要求每条会改变关联的路径都记得配反向操作，漏一处就永久对不上。
    return True


async def _resolve_tags(
    session: AsyncSession, item: ExtractedItem, cache: BatchCache | None = None
) -> list[Tag]:
    """按 item 的标签解析/新建 `Tag` 行，不 flush——新建的行留给调用方
    （`_persist`）跟同一条文档的 media/resource 合并成一次 flush。

    一条 item 常常带好几个标签（类型、画质、年份……），逐个各自 flush
    会让往返次数跟标签数成正比。
    """
    resolved: list[Tag] = []
    for kind, name in item.tags:
        key = tag_norm_key(name)
        if not key:
            continue
        tag_key = (kind, key)

        tag = cache.tag_by_key.get(tag_key) if cache is not None else None
        if tag is None and cache is None:
            tag = await session.scalar(select(Tag).where(Tag.kind == kind, Tag.norm_key == key))
        if tag is None:
            tag = Tag(kind=TagKind(kind), name=name, norm_key=key)
            session.add(tag)
            if cache is not None:
                cache.tag_by_key[tag_key] = tag
        resolved.append(tag)
    return resolved


async def _link_tags(
    session: AsyncSession,
    media: Media,
    tags: list[Tag],
    cache: BatchCache | None = None,
    pending: list[dict] | None = None,
) -> int:
    """建立作品 ↔ 标签关联。返回新建的关联数。

    调用时 `tags` 里的行必须已经 flush 过、拿到了真实 id（见 `_resolve_tags`）。
    新增的关联行同 `_link_media_resource`——传了 `pending` 就攒进去，由调用方
    一次性批量 INSERT，而不是每个标签各发一次往返。
    """
    linked = 0
    pair_rows: list[dict] = []
    for tag in tags:
        pair = (media.id, tag.id)
        if cache is not None:
            exists = pair in cache.media_tag_pairs
        else:
            exists = (
                await session.scalar(
                    select(media_tag.c.tag_id).where(
                        media_tag.c.media_id == media.id, media_tag.c.tag_id == tag.id
                    )
                )
            ) is not None
        if exists:
            continue
        pair_rows.append({"media_id": media.id, "tag_id": tag.id, "created_at": utcnow()})
        tag.media_count += 1
        if cache is not None:
            cache.media_tag_pairs.add(pair)
        linked += 1

    if pending is not None:
        pending.extend(pair_rows)
    elif pair_rows:
        await session.execute(media_tag.insert(), pair_rows)

    return linked


@dataclass(slots=True)
class _PersistState:
    """`_persist_phase1` 产出的中间态，供 `_persist_phase2` 建关联关系。"""

    media_by_item: list[tuple[Media, list[Tag]]]
    resource_by_link: list[tuple[Media, Resource]]


async def _persist_phase1(
    session: AsyncSession,
    doc: RawDocument,
    outcome: ExtractionOutcome,
    report: ParseReport,
    cache: BatchCache | None = None,
) -> _PersistState:
    """阶段一：只 `session.add()` 新建的 media/resource/tag，不 flush。

    调用方（`_persist` 单文档场景、`persist_extracted` 批量场景）决定什么
    时候统一 flush——批量场景把好几条文档的阶段一攒在一起、只 flush 一次，
    才能把「一条文档一次往返」摊薄成「一批文档一次往返」。
    """
    media_by_item: list[tuple[Media, list[Tag]]] = []
    resource_by_link: list[tuple[Media, Resource]] = []

    for item in outcome.items:
        media, created = await _upsert_media(session, item, cache)
        report.media_created += int(created)
        report.media_reused += int(not created)
        tags = await _resolve_tags(session, item, cache)
        media_by_item.append((media, tags))
        for link in item.links:
            resource, is_new = await _upsert_resource(
                session, link, doc=doc, item=item, cache=cache
            )
            report.resources_created += int(is_new)
            report.resources_updated += int(not is_new)
            resource_by_link.append((media, resource))

    # 没归属到作品的链接照样入库（无任何关联），进人工/二次归属队列，绝不丢弃
    for link in outcome.unattributed_links:
        _, is_new = await _upsert_resource(session, link, doc=doc, item=None, cache=cache)
        report.resources_created += int(is_new)
        report.resources_updated += int(not is_new)
    report.unattributed_links = len(outcome.unattributed_links)

    return _PersistState(media_by_item=media_by_item, resource_by_link=resource_by_link)


async def _persist_phase2(
    session: AsyncSession,
    state: _PersistState,
    report: ParseReport,
    cache: BatchCache | None,
    pending_links: list[dict],
    pending_tag_links: list[dict],
) -> set[int]:
    """阶段二：用阶段一 flush 后拿到的 id 建关联关系，攒进调用方共享的批量列表。

    调用方负责在处理完一批文档后把 `pending_links`/`pending_tag_links` 各批量
    INSERT 一次——攒的范围越大（单文档 vs 一整个 SAVEPOINT 里的好几条文档），
    往返就摊得越薄。
    """
    touched: set[int] = set()
    for media, tags in state.media_by_item:
        report.tags_linked += await _link_tags(session, media, tags, cache, pending_tag_links)
        touched.add(media.id)
    for media, resource in state.resource_by_link:
        # 一个链接可以关联多部作品（合集），关联表的唯一约束保证不重复
        report.links_created += int(
            await _link_media_resource(session, media, resource, cache, pending_links)
        )
    return touched


async def _persist(
    session: AsyncSession,
    doc: RawDocument,
    outcome: ExtractionOutcome,
    report: ParseReport,
    cache: BatchCache | None = None,
) -> set[int]:
    """单文档场景的两阶段落库封装，往返次数摊平成固定几次。

    一条文档有 N 个链接、M 个标签，就不该有 N/M 次数据库往返，会让合集帖
    （一贴好几个网盘链接、好几个标签）的落库时间跟链接/标签数成正比，在
    往返延迟 ~100ms 的远程库上非常致命。`persist_extracted` 的批量场景不走
    这个封装，而是把好几条文档的阶段一/阶段二分别攒在一起，摊得更薄——
    见该函数内部对 `_persist_phase1`/`_persist_phase2` 的直接调用。
    """
    state = await _persist_phase1(session, doc, outcome, report, cache)
    await session.flush()

    pending_links: list[dict] = []
    pending_tag_links: list[dict] = []
    touched = await _persist_phase2(session, state, report, cache, pending_links, pending_tag_links)

    if pending_links:
        await session.execute(media_resource.insert(), pending_links)
    if pending_tag_links:
        await session.execute(media_tag.insert(), pending_tag_links)

    return touched


async def parse_document(
    session: AsyncSession,
    doc: RawDocument,
    extractor: Extractor,
    *,
    force: bool = False,
) -> ParseReport:
    """解析一条原始文本，产出 media 与 resource。

    对抽取器的具体实现无感知 —— 规则抽取器和 LLM 抽取器走的是同一条路径。

    Args:
        force: 忽略缓存，强制重新抽取。版本没升但想重跑时用。
    """
    report = ParseReport(document_id=doc.id, status=doc.parse_status)
    now = utcnow()

    try:
        # 用 SAVEPOINT 把"产出并落库这一条文档的抽取结果"框起来：调用方
        # （cli.py 的分批提交、worker 的整批共享 session）可能在同一个外层
        # 事务里连续处理很多条文档。这条文档写到一半炸掉（死锁、并发撞
        # 唯一约束）时，只回滚这个 SAVEPOINT，不会把外层事务标脏、级联
        # 拖垮同一事务里其余文档（此前没有嵌套事务时，一条死锁会让整个
        # chunk 后续文档全部报 InFailedSQLTransactionError，直至整批崩溃）。
        async with session.begin_nested():
            cached = (
                None
                if force
                else await _load_cached(session, doc.id, extractor.name, extractor.version)
            )
            if cached is not None:
                # 命中缓存：同一抽取器 + 同一版本不重复调用外部服务
                outcome = extractor.rehydrate(cached.output, doc.content)
                report.from_cache = True
            else:
                outcome = await extractor.extract(doc.content)
                session.add(
                    Extraction(
                        raw_document_id=doc.id,
                        model=outcome.extractor_name or extractor.name,
                        prompt_version=outcome.extractor_version or extractor.version,
                        output=outcome.raw_payload,
                        input_tokens=outcome.input_tokens,
                        output_tokens=outcome.output_tokens,
                        latency_ms=outcome.latency_ms,
                        stats=outcome.stats,
                    )
                )
                # 不在这里单独 flush——留给 `_persist` 阶段一那次 flush 一并落库。

            report.is_catalog = outcome.is_catalog
            touched = await _persist(session, doc, outcome, report)
            await refresh_media_counters(session, touched)

        # 目录帖不代表一部作品，标为 skipped 而非 done —— 让它在统计里可区分
        doc.parse_status = ParseStatus.SKIPPED if report.is_catalog else ParseStatus.DONE
        doc.parse_error = None
        doc.lease_until = None
        doc.next_parse_at = None
        doc.last_parsed_at = now
        report.status = doc.parse_status

    except IntegrityError:
        # 并发时另一个协程/进程抢先建了同一个 (norm_key, media_type, year) /
        # (provider, share_id) / (kind, norm_key)——不是这条文档本身有问题，
        # SAVEPOINT 已自动回滚，不计入 parse_attempts，留到下次自然重试。
        # 同样不算一次"处理过"：last_parsed_at 不动，下次仍按原优先级排队。
        report.status = doc.parse_status
        report.error = "并发写入冲突，已回滚，留待下次重试（不计入失败次数）"
        logger.info("解析撞车 doc=%s: 并发写入冲突，留待下次重试", doc.id)

    except Exception as exc:
        doc.parse_attempts += 1
        doc.parse_error = f"{type(exc).__name__}: {exc}"
        doc.lease_until = None
        doc.last_parsed_at = now
        if doc.parse_attempts >= MAX_PARSE_ATTEMPTS:
            doc.parse_status = ParseStatus.FAILED
            doc.next_parse_at = None
        else:
            doc.parse_status = ParseStatus.PENDING
            doc.next_parse_at = now + backoff(doc.parse_attempts)
        report.status = doc.parse_status
        report.error = doc.parse_error
        logger.warning("解析失败 doc=%s: %s", doc.id, doc.parse_error)

    return report


async def persist_extracted(
    session: AsyncSession,
    docs: list[RawDocument],
    outcomes: dict[int, ExtractionOutcome],
    cached_doc_ids: set[int],
    extractor: Extractor,
    *,
    extraction_errors: dict[int, str] | None = None,
) -> list[ParseReport]:
    """把一批**已经产出**的抽取结果批量落库。要求 `docs` 已按同一个 extractor 分组。

    从 `parse_batch` 拆出来的"落库"半段——`parse_batch` 自己做抽取再调用这里；
    `services/extract/concurrent_runner.py` 的消费者线程复用同一份逻辑，
    抽取已经在处理单元线程池里做完，这里只管批量预读去重键、逐文档落库、
    状态机推进、计数重算。

    Args:
        cached_doc_ids: 命中缓存（走 `rehydrate` 而非 `extract`）的文档 id。
            这些文档不新建 `Extraction` 留档行。
        extraction_errors: 文档 id → 错误描述，供上游（如并发处理单元）报告
            "这条文档在抽取阶段就失败了、根本没有 outcome"——这类文档直接按
            现有的失败退避逻辑推进状态机，不进入落库流程。

    失败隔离按 `SAVEPOINT_BATCH_SIZE` 条一组：同组文档共享一个 SAVEPOINT、
    一次 flush，把往返次数摊薄成 O(文档数 / SAVEPOINT_BATCH_SIZE)。代价是
    隔离变粗——一条文档撞唯一约束（`IntegrityError`）会连累同组其余文档一起
    回滚重试（不计入失败次数）；其它异常则只把"引发异常那一条"计入失败次数
    /退避，同组其余文档视为受牵连，同样回滚重试、不计入失败次数。
    """
    reports = {doc.id: ParseReport(document_id=doc.id, status=doc.parse_status) for doc in docs}
    now = utcnow()

    cache = await _preload_batch_cache(session, list(outcomes.values()))
    all_touched: set[int] = set()

    persistable_docs: list[RawDocument] = []
    for doc in docs:
        report = reports[doc.id]

        extraction_error = extraction_errors.get(doc.id) if extraction_errors else None
        if extraction_error is not None:
            doc.parse_attempts += 1
            doc.parse_error = extraction_error
            doc.lease_until = None
            doc.last_parsed_at = now
            if doc.parse_attempts >= MAX_PARSE_ATTEMPTS:
                doc.parse_status = ParseStatus.FAILED
                doc.next_parse_at = None
            else:
                doc.parse_status = ParseStatus.PENDING
                doc.next_parse_at = now + backoff(doc.parse_attempts)
            report.status = doc.parse_status
            report.error = doc.parse_error
            logger.warning("解析失败 doc=%s: %s", doc.id, doc.parse_error)
            continue

        persistable_docs.append(doc)

    for start in range(0, len(persistable_docs), SAVEPOINT_BATCH_SIZE):
        chunk = persistable_docs[start : start + SAVEPOINT_BATCH_SIZE]
        await _persist_chunk(
            session, chunk, outcomes, cached_doc_ids, extractor, cache, reports, now, all_touched
        )

    if all_touched:
        await refresh_media_counters(session, all_touched)

    return [reports[doc.id] for doc in docs]


async def _persist_chunk(
    session: AsyncSession,
    chunk: list[RawDocument],
    outcomes: dict[int, ExtractionOutcome],
    cached_doc_ids: set[int],
    extractor: Extractor,
    cache: BatchCache | None,
    reports: dict[int, ParseReport],
    now: Any,
    all_touched: set[int],
) -> None:
    """把一组文档打包进一个共享 SAVEPOINT：阶段一全组 add 完再统一 flush 一次，
    阶段二全组的关联行攒成一批 INSERT，往返次数固定不随组内文档数增长。
    """
    failed_doc_id: int | None = None
    try:
        async with session.begin_nested():
            states: list[tuple[RawDocument, _PersistState]] = []
            for doc in chunk:
                failed_doc_id = doc.id
                report = reports[doc.id]
                outcome = outcomes[doc.id]
                if doc.id in cached_doc_ids:
                    report.from_cache = True
                else:
                    session.add(
                        Extraction(
                            raw_document_id=doc.id,
                            model=outcome.extractor_name or extractor.name,
                            prompt_version=outcome.extractor_version or extractor.version,
                            output=outcome.raw_payload,
                            input_tokens=outcome.input_tokens,
                            output_tokens=outcome.output_tokens,
                            latency_ms=outcome.latency_ms,
                            stats=outcome.stats,
                        )
                    )
                    # 不在这里单独 flush——留给下面全组共享的那一次 flush。
                report.is_catalog = outcome.is_catalog
                state = await _persist_phase1(session, doc, outcome, report, cache)
                states.append((doc, state))

            # 阶段一到此结束：一次 flush 把这一组文档新建的
            # Extraction/media/resource/tag 全部落库，才能拿到它们的 id。
            await session.flush()

            pending_links: list[dict] = []
            pending_tag_links: list[dict] = []
            for doc, state in states:
                failed_doc_id = doc.id
                report = reports[doc.id]
                touched = await _persist_phase2(
                    session, state, report, cache, pending_links, pending_tag_links
                )
                all_touched.update(touched)

            if pending_links:
                await session.execute(media_resource.insert(), pending_links)
            if pending_tag_links:
                await session.execute(media_tag.insert(), pending_tag_links)

        for doc in chunk:
            report = reports[doc.id]
            doc.parse_status = ParseStatus.SKIPPED if report.is_catalog else ParseStatus.DONE
            doc.parse_error = None
            doc.lease_until = None
            doc.next_parse_at = None
            doc.last_parsed_at = now
            report.status = doc.parse_status

    except IntegrityError:
        # 并发撞车不算"处理过"，同组全部回滚，留到下次自然重试。
        for doc in chunk:
            report = reports[doc.id]
            report.status = doc.parse_status
            report.error = "同批并发写入冲突，已回滚，留待下次重试（不计入失败次数）"
        logger.info("解析撞车 docs=%s: 同批并发写入冲突，留待下次重试", [d.id for d in chunk])

    except Exception as exc:
        for doc in chunk:
            report = reports[doc.id]
            if doc.id == failed_doc_id:
                doc.parse_attempts += 1
                doc.parse_error = f"{type(exc).__name__}: {exc}"
                doc.lease_until = None
                doc.last_parsed_at = now
                if doc.parse_attempts >= MAX_PARSE_ATTEMPTS:
                    doc.parse_status = ParseStatus.FAILED
                    doc.next_parse_at = None
                else:
                    doc.parse_status = ParseStatus.PENDING
                    doc.next_parse_at = now + backoff(doc.parse_attempts)
                report.status = doc.parse_status
                report.error = doc.parse_error
                logger.warning("解析失败 doc=%s: %s", doc.id, doc.parse_error)
            else:
                report.status = doc.parse_status
                report.error = "同批其它文档处理异常，已回滚，留待下次重试（不计入失败次数）"
                logger.info("解析连带回滚 doc=%s: 同批其它文档异常，留待下次重试", doc.id)


async def parse_batch(
    session: AsyncSession,
    docs: list[RawDocument],
    extractor: Extractor,
    *,
    force: bool = False,
) -> list[ParseReport]:
    """一批文档共用一次批量预读，逐条落库。要求 `docs` 已按同一个 extractor 分组。

    与循环调用 `parse_document` 的行为差异只有两处：

    1. 缓存查询、media/resource/tag 去重查询批量预读（见 `BatchCache`），
       单条文档落库时只剩 SAVEPOINT + 真正需要的 INSERT，不再逐条查库。
    2. `refresh_media_counters` 在整批结束后对本批触碰到的所有作品统一调用
       一次，而不是每条文档各调用一次。

    落库部分见 `persist_extracted`；这里只负责抽取（命中缓存走 `rehydrate`，
    否则调用 `extractor.extract`）。
    """
    cached_by_doc = (
        {}
        if force
        else await _load_cached_batch(
            session, [doc.id for doc in docs], extractor.name, extractor.version
        )
    )

    outcomes: dict[int, ExtractionOutcome] = {}
    for doc in docs:
        cached = cached_by_doc.get(doc.id)
        outcomes[doc.id] = (
            extractor.rehydrate(cached.output, doc.content)
            if cached is not None
            else await extractor.extract(doc.content)
        )

    return await persist_extracted(session, docs, outcomes, set(cached_by_doc), extractor)
