"""解析流水线：抽取 → 归一 → 落库。

对外两个入口：`parse_document` 处理单条，`parse_batch` 处理一批（同一
extractor）。二者共用同一套幂等 upsert / 状态机推进 / 失败退避逻辑，区别
只在于要不要用 `BatchCache` 把 media/resource/tag 的去重查询从"每条文档
一次往返"折叠成"每批几次往返"——单条 SELECT 在远程数据库上一次就要
~100~150ms，一条文档要查好几次（缓存命中、media 去重、resource 去重、
tag 去重、关联是否已存在……），批量预读把这些查询从 O(条数) 降到 O(1)。
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

from farlog import getLogger
from sqlalchemy import and_, case, or_, select, tuple_, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
    Work,
    media_resource,
    media_tag,
    utcnow,
)
from funflix.models.base import uuid7
from funflix.models.canon import TitleCanon
from funflix.models.media import NO_SEASON, UNKNOWN_YEAR
from funflix.models.raw import PARSE_RULES_VERSION
from funflix.services.canon.lookup import CanonTarget, pending_row, resolve_target
from funflix.services.counters import refresh_counters_for_media
from funflix.services.extract.base import ExtractedItem, ExtractionOutcome, Extractor
from funflix.services.text.linkscan import ScannedLink
from funflix.services.text.normalize import series_norm_key, tag_norm_key

logger = getLogger("funflix")

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

#: 一组撞上并发写入冲突时，本轮里总共跑几次（含第一次）。
#:
#: 只重跑一次就够：报错的时候对面那条已经提交完了，重跑时 `_upsert_*` 会查中
#: 它、走复用分支。还是撞，多半说明冲突来自**组内**（同一批里两个项算出了同一个
#: 身份键却没共用缓存条目），那是我们自己的 bug，再重跑多少次结果都一样。
#:
#: 为什么不干脆留给下一轮：那意味着这一组整整两小时不动（parse 的 cron 是
#: 2 小时一轮）。实测 20000 份一轮里有 560 份（2.8%，28 组）栽在撞车上回滚，
#: 四个分片并行写同一批 `resource`，这个比例随分片数还会涨。
CHUNK_CONFLICT_ATTEMPTS = 2


@dataclass(slots=True)
class ParseReport:
    """单条文档解析落库后的结果摘要：最终状态 + 各类新建/复用计数 + 错误信息。

    由 `parse_document`/`persist_extracted` 等函数在落库过程中逐步填充字段，
    调用方（CLI 进度展示、worker 统计）据此汇总一批文档的处理结果。
    """

    document_id: uuid.UUID
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
        """本次解析是否成功（没有记录错误信息）。"""
        return self.error is None


@dataclass(slots=True)
class BatchCache:
    """一批文档共用的去重键缓存。

    只缓存"这一批已经查到/新建过"的行——不是全局缓存，每次 `parse_batch`
    调用都是一份新的。命中就跳过 SELECT，未命中仍然退化成单条查询/插入，
    正确性与不传 cache 时完全一致，只是把重复查询摊掉。
    """

    #: (work_id, season) → Media。这就是 media 的身份，见 `models/media.py`。
    media_by_key: dict[tuple[uuid.UUID, int], Media] = field(default_factory=dict)
    #: Work.norm_key → Work
    work_by_key: dict[str, Work] = field(default_factory=dict)
    #: `series_norm_key(item.title)` → 裁决行。**值为 None 表示"查过，库里没有"**
    #: —— 和"没查过"必须区分开，否则同一批里的同一个新键会被反复 SELECT，
    #: 而且 `_upsert_media` 会给它插好几行 pending（撞主键）。
    #:
    #: 键空间是 `series_norm_key` 而**不是** `item.norm_key`。`title_canon.norm_key`
    #: 这一列由 `canon/resolver.py` 写入，写的是 `series_norm_key`（见那里的
    #: `_scan_blocks`），`canon/apply.py::_media_ids_for_keys` 回头也按
    #: `series_norm_key(media.title)` 匹配。这里曾经用 `item.norm_key`（逐标题身份，
    #: 不剥季、不剥外文原名、不收敛重复 token），于是防回退查询经常查不中
    #: 已裁决的行 —— 花钱得出的结论入库时被静默绕过，正是这一层要防的事。
    canon_by_key: dict[str, TitleCanon | None] = field(default_factory=dict)
    resource_by_key: dict[tuple[str, str], Resource] = field(default_factory=dict)
    tag_by_key: dict[tuple[str, str], Tag] = field(default_factory=dict)
    media_resource_pairs: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)
    media_tag_pairs: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)


def _snapshot_cache(cache: BatchCache) -> tuple:
    """浅拷贝 `BatchCache` 各容器，供单个 chunk 失败时回滚用。

    只拷贝容器本身（dict/list/set 的壳），不深拷贝里面的 ORM 对象——
    对已经在 chunk 开始前就存在的对象，SAVEPOINT 回滚会由 SQLAlchemy
    自己把它们过期掉（**过期之后必须靠 `_refresh_stale` 显式读回来**，
    异步会话里读过期对象的属性是 `MissingGreenlet` 而不是自动重查，
    见那个函数）；这里要防的
    是"这个 chunk 里新建、又被这个 chunk 的回滚撤销"的对象继续赖在缓存里
    被后面的 chunk 复用——那种对象在库里根本没有对应行，被后面的 chunk 当成
    "已存在"拿去用，关联表 INSERT 就会撞外键；它们的属性在回滚后读出来也
    全是 None。
    """
    return (
        dict(cache.media_by_key),
        dict(cache.work_by_key),
        dict(cache.canon_by_key),
        dict(cache.resource_by_key),
        dict(cache.tag_by_key),
        set(cache.media_resource_pairs),
        set(cache.media_tag_pairs),
    )


def _restore_cache(cache: BatchCache, snapshot: tuple) -> None:
    """把 `cache` 的容器换回 `_snapshot_cache` 之前的状态。"""
    (
        cache.media_by_key,
        cache.work_by_key,
        cache.canon_by_key,
        cache.resource_by_key,
        cache.tag_by_key,
        cache.media_resource_pairs,
        cache.media_tag_pairs,
    ) = snapshot


async def _refresh_stale(session: AsyncSession, cache: BatchCache) -> int:
    """把缓存里过期的 ORM 对象重新读一遍，读不回来的摘掉。返回处理了几条。
    **回滚之后必须调。**

    `_snapshot_cache` 的文档里说「chunk 开始前就存在的对象，SAVEPOINT 回滚
    会由 SQLAlchemy 自己把它们过期掉，下次访问自动重新 SELECT，值总是对的」
    —— 前半句对，**后半句在异步会话里是错的**。属性访问是同步的，它没法
    `await` 那次重新 SELECT，于是直接抛
    `MissingGreenlet: greenlet_spawn has not been called`。

    后果是「撞车重跑」这条路**必然**失败：`_persist_chunk` 回滚后调用方原样
    重跑同一组（见 `persist_extracted`），重跑时 `_upsert_*` 在缓存里查中一个
    已过期的对象、读它的 `id` 就炸。run 37778092602 的 parse (4) 里 39 次撞车
    有 18 次这样炸掉，每次连带整组回滚，一个分片就白烧 360 份文档。

    所以在这里**显式** `await session.refresh(...)` 把它们读回来 —— 这是整个
    回滚路径上唯一还在协程里、能 await 的地方，错过这里就只剩同步属性访问。

    **不能图省事直接从缓存里摘掉。** `_upsert_media`/`_upsert_work`/
    `_upsert_resource`/`_upsert_tag` 传了 `cache` 时**缓存就是权威**：未命中
    不回落 SELECT，直接按「库里没有」新建一行。摘掉一个其实存在的键，重跑时
    就是插重复行 —— 实测报
    `UNIQUE constraint failed: resource.provider, resource.share_id`，撞车重跑
    照样整组回滚，只是把 `MissingGreenlet` 换成了唯一键冲突。

    读不回来的（refresh 抛 `ObjectDeletedError`，或对象已经游离/被判为
    transient）才摘掉：那种情况库里确实没有对应行，让 `_upsert_*` 重新建
    才是对的。正常情况下这一类已经被 `_restore_cache` 撤掉了（它们是本
    chunk 新建的，不在快照里），这里只是兜底。
    """
    handled = 0
    for mapping in (
        cache.media_by_key,
        cache.work_by_key,
        cache.canon_by_key,
        cache.resource_by_key,
        cache.tag_by_key,
    ):
        for key, obj in list(mapping.items()):
            if obj is None or not _is_stale(obj):
                continue
            handled += 1
            state = sa_inspect(obj)
            if state.persistent or state.deleted:
                try:
                    await session.refresh(obj)
                    continue
                except SQLAlchemyError:
                    # 行真的没了（别的节点删掉了），只能摘。
                    pass
            del mapping[key]
    return handled


def _is_stale(obj: Any) -> bool:
    """这个 ORM 对象现在读属性会不会触发 IO。全是内存里的判断，自己不碰库。"""
    state = sa_inspect(obj)
    return bool(state.expired or state.detached or state.transient or state.deleted)


def keyset_after(ts_col: Any, id_col: Any, last_ts: Any, last_id: uuid.UUID) -> Any:
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
    session: AsyncSession, doc_id: uuid.UUID, name: str, version: str
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
    session: AsyncSession, doc_ids: list[uuid.UUID], name: str, version: str
) -> dict[uuid.UUID, Extraction]:
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

    canon/work/media/resource/tag 各一次查询，外加"这些已存在的 media 都
    关联了哪些 resource/tag"再各一次——七次往返覆盖整批（可能几百条文档），
    而不是每条文档各查一遍。

    **work 必须在 media 之前预读**：media 的身份是 `(work_id, season)`，
    没有 work_id 就无从按身份去查 media。而 work 的候选键有两个来源 ——
    `title_canon` 裁决出的作品键，以及规则现算的 `series_norm_key` ——
    所以 canon 又得排在 work 前面。这个顺序和 `_upsert_media` 里的决策顺序
    是同一条链，不能打乱。
    """
    cache = BatchCache()

    item_keys: set[str] = set()
    item_titles: set[str] = set()
    provider_shares: set[tuple[str, str]] = set()
    tag_keys: set[tuple[str, str]] = set()

    for outcome in outcomes:
        for item in outcome.items:
            item_keys.add(series_norm_key(item.title))
            item_titles.add(item.title)
            for kind, name in item.tags:
                key = tag_norm_key(name)
                if key:
                    tag_keys.add((kind, key))
            for link in item.links:
                provider_shares.add((link.provider, link.share_id))
        for link in outcome.unattributed_links:
            provider_shares.add((link.provider, link.share_id))

    # 标题被洗成空的那些项（纯噪声）算出空键，不值得去查 —— 它们在
    # `_upsert_media` 里会因为 `work_norm_key` 为空被整项丢掉。
    item_keys.discard("")
    if item_keys:
        rows = await session.scalars(select(TitleCanon).where(TitleCanon.norm_key.in_(item_keys)))
        for canon in rows:
            cache.canon_by_key[canon.norm_key] = canon
        # 没查到的键显式记成 None —— 见 `BatchCache.canon_by_key` 的说明。
        for key in item_keys:
            cache.canon_by_key.setdefault(key, None)

    work_keys = {series_norm_key(title) for title in item_titles}
    work_keys |= {
        canon.work_norm_key
        for canon in cache.canon_by_key.values()
        if canon is not None and canon.work_norm_key
    }
    work_keys.discard("")
    if work_keys:
        rows = await session.scalars(select(Work).where(Work.norm_key.in_(work_keys)))
        for work in rows:
            cache.work_by_key[work.norm_key] = work

    work_ids = [work.id for work in cache.work_by_key.values()]
    if work_ids:
        # 一次把这些作品**所有**的季拉回来，不按 `(work_id, season)` 逐对查。
        # 一部剧的季数是个位数，整组拉回来比拼一个大 tuple IN 便宜，也省得
        # 规则和裁决对季号判断不一致时漏命中。
        rows = await session.scalars(select(Media).where(Media.work_id.in_(work_ids)))
        for media in rows:
            cache.media_by_key[(media.work_id, media.season or NO_SEASON)] = media

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


async def _lookup_canon(
    session: AsyncSession, norm_key: str, cache: BatchCache | None
) -> TitleCanon | None:
    """查这个抽取键的归一裁决。查不到返回 None。"""
    if cache is not None and norm_key in cache.canon_by_key:
        return cache.canon_by_key[norm_key]
    canon = await session.get(TitleCanon, norm_key)
    if cache is not None:
        cache.canon_by_key[norm_key] = canon
    return canon


async def _upsert_work(
    session: AsyncSession, target: CanonTarget, cache: BatchCache | None
) -> Work:
    """按 `work_norm_key` get-or-create 作品。

    新建时**显式给 id**（`uuid7()`），不靠 ORM 默认值 —— 默认值要到 flush
    才生效，而紧接着就要用 `(work_id, season)` 当 media 的缓存键。为了拿一个
    id 去 flush 会把「一批文档只 flush 一次」的优化整个作废。

    已存在的作品只补空字段，不覆盖已有值：库里的值可能来自 LLM 裁决或人工
    修订，都比单条分享现判的更可信。
    """
    if cache is not None:
        work = cache.work_by_key.get(target.work_norm_key)
    else:
        work = await session.scalar(select(Work).where(Work.norm_key == target.work_norm_key))

    if work is None:
        work = Work(
            id=uuid7(),
            title=target.work_title[:500] or target.work_norm_key[:500],
            norm_key=target.work_norm_key,
            media_type=target.media_type,
            year=target.year,
            aliases=[],
        )
        session.add(work)
        if cache is not None:
            cache.work_by_key[target.work_norm_key] = work
        return work

    if work.media_type is MediaType.UNKNOWN and target.media_type is not MediaType.UNKNOWN:
        work.media_type = target.media_type
    if work.year == UNKNOWN_YEAR and target.year != UNKNOWN_YEAR:
        work.year = target.year
    return work


async def _upsert_media(
    session: AsyncSession, item: ExtractedItem, cache: BatchCache | None = None
) -> tuple[Media | None, bool]:
    """按 `title_canon` 的裁决把抽取项落到某个作品的某一季。

    返回 `(None, False)` 表示裁决判定这个标题根本不是作品 —— 调用方应把它的
    链接转成未归属资源。

    流程是「查裁决 → 定作品键和季 → get-or-create Work → get-or-create
    (work_id, season)」。**原来那套「类型放宽」的回退删掉了**：它存在的理由
    是身份三元组里有 `media_type`，同一部剧被判成 tv 和 unknown 就会裂成两行。
    现在 media 的身份是 `(work_id, season)`，类型退化成属性，裂不了，
    回退也就没有存在意义。

    传了 `cache` 时优先查内存、未命中才落到 SELECT——批内新建的 work/media
    也会写回 cache，同批后续文档引用同一部作品不会再查一次库。
    """
    # 用 `series_norm_key` 而不是 `item.norm_key` —— 键空间的理由见 `BatchCache`。
    canon_key = series_norm_key(item.title)
    canon = await _lookup_canon(session, canon_key, cache)
    target = resolve_target(
        title=item.title, media_type=item.media_type, year=item.year, canon=canon
    )
    if target.is_junk:
        return None, False

    if target.needs_pending_row and canon is None and canon_key:
        # 新键：写一行 pending 让 `canon resolve` 下次捞走。写回 cache 是为了
        # 同一批里的同一个键不会被插第二行（撞主键会把整个 SAVEPOINT 带崩）。
        #
        # `canon is None` 这个条件是必须的，不能只看 `needs_pending_row`：
        # `resolve_target` 碰到一行 decided 但 `work_norm_key` 是空的坏裁决时会
        # 回落到 `_fallback`，而 `_fallback` 无条件把 `needs_pending_row` 置真 ——
        # 那个键在库里已经有行了，再插一行就是撞主键。
        canon = pending_row(canon_key, target)
        session.add(canon)
        if cache is not None:
            cache.canon_by_key[canon_key] = canon

    if not target.work_norm_key:
        # 规则把标题洗成空了（纯噪声）。这种项在抽取器层本该已经被
        # `looks_like_junk_title` 拦掉，兜一下避免建出空键 Work。
        return None, False

    work = await _upsert_work(session, target, cache)
    identity = (work.id, target.season)

    if cache is not None:
        media = cache.media_by_key.get(identity)
    else:
        media = await session.scalar(
            select(Media).where(Media.work_id == work.id, Media.season == target.season)
        )

    if media is not None:
        if item.title not in media.aliases and item.title != media.title:
            media.aliases = [*media.aliases, item.title]
        if media.original_title is None and item.original_title:
            media.original_title = item.original_title
        if media.media_type is MediaType.UNKNOWN and target.media_type is not MediaType.UNKNOWN:
            media.media_type = target.media_type
        if media.year == UNKNOWN_YEAR and target.year != UNKNOWN_YEAR:
            media.year = target.year
        return media, False

    media = Media(
        work_id=work.id,
        season=target.season,
        title=item.title,
        # `norm_key` 落**作品键**，不再是这一条分享自己的脏键。它已经不参与
        # 身份判定（见 `models/media.py`），留着是为了排查时能一眼看出
        # 这一季挂在哪个作品下。
        norm_key=target.work_norm_key,
        original_title=item.original_title,
        media_type=target.media_type,
        year=target.year,
        aliases=[],
    )
    session.add(media)
    # 不在这里单独 flush——新建的 media 先攒着，跟同一条文档里新建的
    # resource、Extraction 一起，由调用方（`_persist`）一次性 flush，
    # 把「一条文档有 N 个链接就要 N 次数据库往返」降到固定次数。
    if cache is not None:
        cache.media_by_key[identity] = media
    return media, True


async def _upsert_resource(
    session: AsyncSession,
    link: ScannedLink,
    *,
    doc: RawDocument,
    item: ExtractedItem | None,
    cache: BatchCache | None,
    seen_deltas: dict[Resource, int],
) -> tuple[Resource, bool]:
    """按 (provider, share_id) 幂等落库。返回 (资源, 是否新建)。

    重复看到的行**不在这里改 `seen_count`/`last_seen_at`**，只往 `seen_deltas`
    记一笔，由调用方收尾时一条算术 UPDATE 落下去（`_apply_resource_seen_deltas`）。
    理由同 `_apply_tag_count_deltas`：热行上的 ORM 自增既会丢更新，又会把行锁
    一路握到外层事务提交。
    """
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
        seen_deltas[existing] = seen_deltas.get(existing, 0) + 1
        if existing.passcode is None and link.passcode:
            existing.passcode = link.passcode
        if existing.title_raw is None and item is not None:
            existing.title_raw = item.title
        if existing.size_bytes is None and item is not None:
            existing.size_bytes = item.size_bytes
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
        size_bytes=item.size_bytes if item else None,
        check_status=CheckStatus.UNCHECKED if checkable else CheckStatus.UNSUPPORTED,
        next_check_at=now if checkable else None,
        first_seen_at=now,
        last_seen_at=now,
        # 新建就是"第一次看到"。同一批里这份分享再出现时走上面的 existing
        # 分支、只记增量，所以收尾那条 `seen_count = seen_count + :d` 会加在
        # 这个 1 上，不会把它冲掉。
        seen_count=1,
    )
    session.add(resource)
    # 同 `_upsert_media`：不在这里单独 flush，攒到调用方一次性 flush。
    if cache is not None:
        cache.resource_by_key[key] = resource
    return resource, True


async def _apply_resource_seen_deltas(
    session: AsyncSession, deltas: dict[Resource, int], now: Any
) -> None:
    """把"又看到了几次"用**一条**算术 UPDATE 写回 `resource.seen_count`。

    和 `_apply_tag_count_deltas` 是同一套理由、同一套写法，只是慢一步才发现：
    `resource` 有近两百万行，看着不像热行，但分享链接是**被反复转发**的
    —— 爆款那一份会出现在成百上千条文档里。8 个分片进程跑起来之后，
    `pg_blocking_pids` 上排队最长的就变成了
    `UPDATE resource SET last_seen_at=..., seen_count=...`，等了 1 分 55 秒。
    （标签那条已经不在榜上了，说明上一轮改对了，只是把瓶颈让给了下一个。）

    键是 ORM 对象而不是 id：这个增量在**阶段一**攒，那时新建的行还没 flush、
    `id` 是 `uuid7` 的 Python 侧默认值、要到 flush 才赋上。等收尾时再取 `.id`
    就都有了。

    调用方的约束同 `_apply_tag_count_deltas`：只能合并提交成功的那部分。
    """
    if not deltas:
        return
    ordered = sorted(((r.id, d) for r, d in deltas.items()), key=lambda kv: kv[0])
    await session.execute(
        update(Resource)
        .where(Resource.id.in_([rid for rid, _ in ordered]))
        .values(
            seen_count=Resource.seen_count + case(dict(ordered), value=Resource.id),
            # 字典里的行都是"这一批又看到了"，统一盖上这一批的时间戳。
            last_seen_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )


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
            # 显式给 media_count 赋初值。列定义的 `default=0` 也会在 flush 时
            # 补上，写在这里是为了让「计数从 0 起、只由
            # `_apply_tag_count_deltas` 的算术 UPDATE 推动」这件事在建对象的
            # 地方就看得见——否则 flush 之前 Python 侧读出来是 None。
            tag = Tag(kind=TagKind(kind), name=name, norm_key=key, media_count=0)
            session.add(tag)
            if cache is not None:
                cache.tag_by_key[tag_key] = tag
        resolved.append(tag)
    return resolved


async def _link_tags(
    session: AsyncSession,
    media: Media,
    tags: list[Tag],
    cache: BatchCache | None,
    pending: list[dict] | None,
    tag_deltas: dict[uuid.UUID, int],
) -> int:
    """建立作品 ↔ 标签关联。返回新建的关联数。

    调用时 `tags` 里的行必须已经 flush 过、拿到了真实 id（见 `_resolve_tags`）。
    新增的关联行同 `_link_media_resource`——传了 `pending` 就攒进去，由调用方
    一次性批量 INSERT，而不是每个标签各发一次往返。

    **计数不在这里改 `tag.media_count`，只往 `tag_deltas` 记增量**，由调用方
    收尾时用一条算术 UPDATE 落下去（`_apply_tag_count_deltas`）。原因见那个
    函数的 docstring：热行上的 ORM 自增既慢又会丢更新。
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
        tag_deltas[tag.id] = tag_deltas.get(tag.id, 0) + 1
        if cache is not None:
            cache.media_tag_pairs.add(pair)
        linked += 1

    if pending is not None:
        pending.extend(pair_rows)
    elif pair_rows:
        await session.execute(media_tag.insert(), pair_rows)

    return linked


async def _apply_tag_count_deltas(session: AsyncSession, deltas: dict[uuid.UUID, int]) -> None:
    """把攒下来的标签计数增量用**一条**算术 UPDATE 写回 `tag.media_count`。

    这里刻意不走 ORM 的 `tag.media_count += 1`，有两个各自独立的理由：

    **一、丢更新。** ORM 会把 `+= 1` 刷成绝对值（读到 5 就写
    `SET media_count = 6`）。两个 parse 进程同时处理挂了同一个标签的文档，
    后提交的那个就把前一个的 +1 盖掉了。这正是 `maintenance.recount_tags`
    存在的原因。算术形式 `media_count = media_count + :d` 由数据库在持有行锁
    时自己算，不存在这个窗口。

    **二、串行化。** 生产库只有一千多个标签行，而几乎每条文档都会挂上
    「夸克」「电视剧」这类大热标签——ORM 自增发生在阶段二，行锁要一直握到
    外层事务提交（一整批上百条文档、好几秒），于是所有 parse 进程在那几行上
    排队。实测 8 个分片进程跑出来 2.6 条/s，比单进程的 4.6 条/s 还慢，
    `pg_blocking_pids` 上看到的就是 `UPDATE tag SET media_count=...` 的
    `Lock: transactionid` 等待，最长 57 秒。改成收尾时一条语句之后，行锁只从
    这条语句握到提交，窗口是毫秒级。

    **上面那个「8 片比单进程还慢」只是改之前的事实，别再拿它当现行结论。**
    2026-10-08 在 4 片 + 本机一个进程同时写库时采了三次 `pg_stat_activity`：
    `pg_blocking_pids` 全是空的，一个等锁的后端都没有，几乎每个后端都停在
    `Client:ClientRead`（库在等客户端发下一条语句）。两处计数都改完之后锁竞争
    已经不是瓶颈了，CI 的片数也因此从 4 提到了 8（见 `.github/workflows/collect.yml`）。

    调用方必须保证**只有真的提交成功的 chunk 才把增量合并进来**——增量不幂等，
    回滚重试的 chunk 要是也算一份，计数就会偏高。（`all_touched` 没这个要求，
    因为 `refresh_counters_for_media` 是重算。）

    同理，必须在 `refresh_counters_for_media` **之前**调用：那个函数在物理删除
    零资源作品时会按关联表**重算**受影响标签的计数，先加增量再重算是对的
    （重算覆盖掉就行），反过来会在已经正确的值上再加一次。
    """
    if not deltas:
        return
    # 一条 CASE 把整批写完，理由同 `services/counters.py`：远端库往返很贵。
    # id 排序是为了让并发进程取行锁的顺序尽量一致，少踩死锁。
    ordered = sorted(deltas.items())
    await session.execute(
        update(Tag)
        .where(Tag.id.in_([tag_id for tag_id, _ in ordered]))
        .values(
            media_count=Tag.media_count + case(dict(ordered), value=Tag.id),
            updated_at=utcnow(),
        )
        # 身份映射里那些 Tag 对象的 `media_count` 会就此过期，但落库路径之后
        # 不再读它；让 ORM 去同步反而要么多发一次 SELECT、要么在 Python 侧
        # 求值这个 CASE 失败。
        .execution_options(synchronize_session=False)
    )


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
    cache: BatchCache | None,
    seen_deltas: dict[Resource, int],
) -> _PersistState:
    """阶段一：只 `session.add()` 新建的 media/resource/tag，不 flush。

    调用方（`_persist` 单文档场景、`persist_extracted` 批量场景）决定什么
    时候统一 flush——批量场景把好几条文档的阶段一攒在一起、只 flush 一次，
    才能把「一条文档一次往返」摊薄成「一批文档一次往返」。

    `seen_deltas` 由调用方收尾时用一条 UPDATE 落下去，见
    `_apply_resource_seen_deltas`。
    """
    media_by_item: list[tuple[Media, list[Tag]]] = []
    resource_by_link: list[tuple[Media, Resource]] = []
    resources: dict[tuple, Resource] = {}

    # 被归一裁决判成「不是作品」的项，它的链接降级成未归属资源，和抽取器
    # 丢弃目录页时的处理一致 —— 链接本身是真的，只是没有可挂的作品。
    orphaned: list[ScannedLink] = []

    for item in outcome.items:
        media, created = await _upsert_media(session, item, cache)
        if media is None:
            orphaned.extend(item.links)
            continue
        report.media_created += int(created)
        report.media_reused += int(not created)
        tags = await _resolve_tags(session, item, cache)
        media_by_item.append((media, tags))
        for link in item.links:
            resource = resources.get(link.key)
            if resource is None:
                resource, is_new = await _upsert_resource(
                    session, link, doc=doc, item=item, cache=cache, seen_deltas=seen_deltas
                )
                resources[link.key] = resource
                report.resources_created += int(is_new)
                report.resources_updated += int(not is_new)
            resource_by_link.append((media, resource))

    # 没归属到作品的链接照样入库（无任何关联），进人工/二次归属队列，绝不丢弃
    for link in [*outcome.unattributed_links, *orphaned]:
        _, is_new = await _upsert_resource(
            session, link, doc=doc, item=None, cache=cache, seen_deltas=seen_deltas
        )
        report.resources_created += int(is_new)
        report.resources_updated += int(not is_new)
    report.unattributed_links = len(outcome.unattributed_links) + len(orphaned)

    return _PersistState(media_by_item=media_by_item, resource_by_link=resource_by_link)


async def _persist_phase2(
    session: AsyncSession,
    state: _PersistState,
    report: ParseReport,
    cache: BatchCache | None,
    pending_links: list[dict],
    pending_tag_links: list[dict],
    tag_deltas: dict[uuid.UUID, int],
) -> set[uuid.UUID]:
    """阶段二：用阶段一 flush 后拿到的 id 建关联关系，攒进调用方共享的批量列表。

    调用方负责在处理完一批文档后把 `pending_links`/`pending_tag_links` 各批量
    INSERT 一次，并把 `tag_deltas` 用一条 UPDATE 落下去（`_apply_tag_count_deltas`）
    ——攒的范围越大（单文档 vs 一整个 SAVEPOINT 里的好几条文档），往返就摊得越薄。
    """
    touched: set[uuid.UUID] = set()
    for media, tags in state.media_by_item:
        report.tags_linked += await _link_tags(
            session, media, tags, cache, pending_tag_links, tag_deltas
        )
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
) -> set[uuid.UUID]:
    """单文档场景的两阶段落库封装，往返次数摊平成固定几次。

    一条文档有 N 个链接、M 个标签，就不该有 N/M 次数据库往返，会让合集帖
    （一贴好几个网盘链接、好几个标签）的落库时间跟链接/标签数成正比，在
    往返延迟 ~100ms 的远程库上非常致命。`persist_extracted` 的批量场景不走
    这个封装，而是把好几条文档的阶段一/阶段二分别攒在一起，摊得更薄——
    见该函数内部对 `_persist_phase1`/`_persist_phase2` 的直接调用。
    """
    seen_deltas: dict[Resource, int] = {}
    state = await _persist_phase1(session, doc, outcome, report, cache, seen_deltas)
    await session.flush()

    pending_links: list[dict] = []
    pending_tag_links: list[dict] = []
    tag_deltas: dict[uuid.UUID, int] = {}
    touched = await _persist_phase2(
        session, state, report, cache, pending_links, pending_tag_links, tag_deltas
    )

    if pending_links:
        await session.execute(media_resource.insert(), pending_links)
    if pending_tag_links:
        await session.execute(media_tag.insert(), pending_tag_links)
    # 必须在调用方的 `refresh_counters_for_media` 之前，见 `_apply_tag_count_deltas`。
    await _apply_tag_count_deltas(session, tag_deltas)
    await _apply_resource_seen_deltas(session, seen_deltas, utcnow())

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
            await refresh_counters_for_media(session, touched)

        # 目录帖不代表一部作品，标为 skipped 而非 done —— 让它在统计里可区分
        doc.parse_status = ParseStatus.SKIPPED if report.is_catalog else ParseStatus.DONE
        doc.parse_error = None
        doc.lease_until = None
        doc.next_parse_at = None
        doc.last_parsed_at = now
        # 盖上规则集版本戳。`repair requeue` 靠它找出按旧规则解析的文档，
        # 见 `models/raw.py::PARSE_RULES_VERSION`。
        doc.parse_rules_version = PARSE_RULES_VERSION
        report.status = doc.parse_status

    except IntegrityError:
        # 并发时另一个协程/进程抢先建了同一个 (norm_key, media_type, year) /
        # (provider, share_id) / (kind, norm_key)——不是这条文档本身有问题，
        # SAVEPOINT 已自动回滚，不计入 parse_attempts，留到下次自然重试。
        # 同样不算一次"处理过"：last_parsed_at 不动，下次仍按原优先级排队。
        report.status = doc.parse_status
        report.error = "并发写入冲突，已回滚，留待下次重试（不计入失败次数）"
        logger.info(f"解析撞车 doc={doc.id}: 并发写入冲突，留待下次重试")

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
        logger.warning(f"解析失败 doc={doc.id}: {doc.parse_error}")

    return report


async def persist_extracted(
    session: AsyncSession,
    docs: list[RawDocument],
    outcomes: dict[uuid.UUID, ExtractionOutcome],
    cached_doc_ids: set[uuid.UUID],
    extractor: Extractor,
    *,
    extraction_errors: dict[uuid.UUID, str] | None = None,
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
    回滚（不计入失败次数），这一组会在本轮里原样重跑，见
    `CHUNK_CONFLICT_ATTEMPTS`；其它异常则只把"引发异常那一条"计入失败次数
    /退避，同组其余文档视为受牵连，同样回滚、留待下一轮、不计入失败次数。
    """
    reports = {doc.id: ParseReport(document_id=doc.id, status=doc.parse_status) for doc in docs}
    now = utcnow()

    cache = await _preload_batch_cache(session, list(outcomes.values()))
    all_touched: set[uuid.UUID] = set()
    #: 热行计数的增量，都只收提交成功的 chunk 的那一份
    #: （见 `_apply_tag_count_deltas` / `_apply_resource_seen_deltas`）。
    tag_deltas: dict[uuid.UUID, int] = {}
    seen_deltas: dict[Resource, int] = {}

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
            logger.warning(f"解析失败 doc={doc.id}: {doc.parse_error}")
            continue

        persistable_docs.append(doc)

    for start in range(0, len(persistable_docs), SAVEPOINT_BATCH_SIZE):
        chunk = persistable_docs[start : start + SAVEPOINT_BATCH_SIZE]
        # 报告的计数是**原地累加**的（`report.media_created += ...`），重跑前
        # 得先留一份原样，不然重试过的那一组计数会翻倍。
        snapshot = {doc.id: copy.copy(reports[doc.id]) for doc in chunk}
        for attempt in range(CHUNK_CONFLICT_ATTEMPTS):
            conflicted = await _persist_chunk(
                session,
                chunk,
                outcomes,
                cached_doc_ids,
                extractor,
                cache,
                reports,
                now,
                all_touched,
                tag_deltas,
                seen_deltas,
            )
            if not conflicted or attempt == CHUNK_CONFLICT_ATTEMPTS - 1:
                break
            # 撞车的那一行**现在已经在库里了**（对面提交完了才轮到我们报错），
            # 而 `_persist_chunk` 回滚时把这一组写进 `cache` 的条目撤了、
            # 把回滚打过期的条目重新读了回来（`_refresh_stale`，少了这一步
            # 重跑必然抛 `MissingGreenlet`），于是重跑时 `_upsert_*` 要么查中
            # 刷新后的那一行、要么重新 SELECT，都走复用分支 —— 这才是"下一轮
            # 重试"真正会发生的事，只是不用等两小时。
            for doc in chunk:
                reports[doc.id] = copy.copy(snapshot[doc.id])
            logger.info(f"解析撞车重跑这一组 {len(chunk)} 份（第 {attempt + 1} 次）")

    # 顺序有要求：标签增量必须先落，`refresh_counters_for_media` 删空作品时会
    # 按关联表重算受影响标签的计数，反过来就会在正确值上再加一遍增量。
    await _apply_tag_count_deltas(session, tag_deltas)
    await _apply_resource_seen_deltas(session, seen_deltas, now)
    if all_touched:
        await refresh_counters_for_media(session, all_touched)

    return [reports[doc.id] for doc in docs]


async def _persist_chunk(
    session: AsyncSession,
    chunk: list[RawDocument],
    outcomes: dict[uuid.UUID, ExtractionOutcome],
    cached_doc_ids: set[uuid.UUID],
    extractor: Extractor,
    cache: BatchCache | None,
    reports: dict[uuid.UUID, ParseReport],
    now: Any,
    all_touched: set[uuid.UUID],
    tag_deltas: dict[uuid.UUID, int],
    seen_deltas: dict[Resource, int],
) -> bool:
    """把一组文档打包进一个共享 SAVEPOINT：阶段一全组 add 完再统一 flush 一次，
    阶段二全组的关联行攒成一批 INSERT，往返次数固定不随组内文档数增长。

    返回「这一组是不是因为并发写入冲突整体回滚了」—— 真值时调用方可以在本轮
    里原样重跑一次（见 `persist_extracted`），别的情况都是假值（提交成功，或者
    已经按文档记好失败/连带回滚，重跑没意义）。

    两个热行计数的增量先攒在 chunk 本地，只在这个 SAVEPOINT 真的提交之后才
    合并进调用方的 `tag_deltas`/`seen_deltas`——增量不幂等，回滚重试的 chunk
    多算一份计数就偏高了。（`all_touched` 没这个顾虑，重算多传几个 id 只是白跑。）
    """
    failed_doc_id: uuid.UUID | None = None
    chunk_tag_deltas: dict[uuid.UUID, int] = {}
    chunk_seen_deltas: dict[Resource, int] = {}
    cache_snapshot = _snapshot_cache(cache) if cache is not None else None
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
                state = await _persist_phase1(
                    session, doc, outcome, report, cache, chunk_seen_deltas
                )
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
                    session,
                    state,
                    report,
                    cache,
                    pending_links,
                    pending_tag_links,
                    chunk_tag_deltas,
                )
                all_touched.update(touched)

            if pending_links:
                await session.execute(media_resource.insert(), pending_links)
            if pending_tag_links:
                await session.execute(media_tag.insert(), pending_tag_links)

        # SAVEPOINT 已提交，这一组的计数增量才算真的发生了。
        for tag_id, delta in chunk_tag_deltas.items():
            tag_deltas[tag_id] = tag_deltas.get(tag_id, 0) + delta
        for resource, delta in chunk_seen_deltas.items():
            seen_deltas[resource] = seen_deltas.get(resource, 0) + delta

        for doc in chunk:
            report = reports[doc.id]
            doc.parse_status = ParseStatus.SKIPPED if report.is_catalog else ParseStatus.DONE
            doc.parse_error = None
            doc.lease_until = None
            doc.next_parse_at = None
            doc.last_parsed_at = now
            doc.parse_rules_version = PARSE_RULES_VERSION
            report.status = doc.parse_status

    except IntegrityError:
        # 并发撞车不算"处理过"，同组全部回滚。
        # 缓存也要跟着回滚——这个 chunk 里新建、写进 cache 的对象已经随
        # SAVEPOINT 一起失效，留着会被下一个 chunk 当"已存在"复用到坏对象。
        if cache is not None and cache_snapshot is not None:
            _restore_cache(cache, cache_snapshot)
            # 把回滚打过期的对象读回来，否则下面这一组原样重跑时会撞
            # `MissingGreenlet` —— 见 `_refresh_stale`。
            await _refresh_stale(session, cache)
        for doc in chunk:
            report = reports[doc.id]
            report.status = doc.parse_status
            report.error = "同批并发写入冲突，已回滚，留待下次重试（不计入失败次数）"
        logger.info(f"解析撞车 docs={[d.id for d in chunk]}: 同批并发写入冲突")
        # 由调用方决定还要不要在本轮里再试一次，见 `persist_extracted`。
        return True

    except Exception as exc:
        # 同上：这个 chunk 的 SAVEPOINT 整体回滚了，缓存里这个 chunk 期间
        # 新增/新建的条目也得跟着撤销，不然下一个 chunk 会复用到已经失效
        # 的 ORM 对象（比如新建的 Tag，回滚后库里没有对应行，下一个 chunk
        # 把它当"已存在"用，`media_tag` 的 INSERT 就撞外键）。
        # `chunk_tag_deltas`/`chunk_seen_deltas` 不用清：它们是 chunk 本地的，
        # 只有上面提交成功的那条路径才会把它们合并进调用方的字典。
        if cache is not None and cache_snapshot is not None:
            _restore_cache(cache, cache_snapshot)
            # 同上。这条路不重跑当前组，但后面的 chunk 还要接着用这个缓存。
            await _refresh_stale(session, cache)
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
                logger.warning(f"解析失败 doc={doc.id}: {doc.parse_error}")
            else:
                report.status = doc.parse_status
                report.error = "同批其它文档处理异常，已回滚，留待下次重试（不计入失败次数）"
                logger.info(f"解析连带回滚 doc={doc.id}: 同批其它文档异常，留待下次重试")

    return False


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
    2. `refresh_counters_for_media` 在整批结束后对本批触碰到的所有季统一调用
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

    outcomes: dict[uuid.UUID, ExtractionOutcome] = {}
    for doc in docs:
        cached = cached_by_doc.get(doc.id)
        outcomes[doc.id] = (
            extractor.rehydrate(cached.output, doc.content)
            if cached is not None
            else await extractor.extract(doc.content)
        )

    return await persist_extracted(session, docs, outcomes, set(cached_by_doc), extractor)
