"""解析流水线：抽取 → 归一 → 落库。

对外只有 `parse_document` 一个入口。它负责：
缓存复用、状态机推进、media/resource 的幂等 upsert、失败退避。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
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


async def _upsert_media(session: AsyncSession, item: ExtractedItem) -> tuple[Media, bool]:
    """按 (norm_key, media_type, year) 找已有作品，找不到才新建。

    多了一步「类型放宽」的回退：同一部作品在不同分享里可能一次被判成 tv、
    一次判成 unknown。若严格按三元组匹配，它们会变成两部作品。
    所以先精确匹配，再按 (norm_key, year) 放宽，并顺手把 unknown 升级成已知类型。
    """
    year = item.year if item.year is not None else UNKNOWN_YEAR

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
    await session.flush()
    return media, True


async def _upsert_resource(
    session: AsyncSession,
    link: ScannedLink,
    *,
    doc: RawDocument,
    item: ExtractedItem | None,
) -> tuple[Resource, bool]:
    """按 (provider, share_id) 幂等落库。返回 (资源, 是否新建)。"""
    now = utcnow()
    existing = await session.scalar(
        select(Resource).where(
            Resource.provider == link.provider, Resource.share_id == link.share_id
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
    await session.flush()
    return resource, True


async def _link_media_resource(session: AsyncSession, media: Media, resource: Resource) -> bool:
    """建立作品 ↔ 资源关联。已存在则跳过。返回 True 表示新建了关联。

    先查后插而不是靠数据库的 ON CONFLICT —— 后者语法各方言不同，
    而 schema 要同时跑在 SQLite 和 PostgreSQL 上。
    """
    exists = await session.scalar(
        select(media_resource.c.media_id).where(
            media_resource.c.media_id == media.id,
            media_resource.c.resource_id == resource.id,
        )
    )
    if exists is not None:
        return False

    await session.execute(
        media_resource.insert().values(
            media_id=media.id, resource_id=resource.id, created_at=utcnow()
        )
    )
    # 计数不在这里 +1：`_persist` 收尾时按关联表重算一次。
    # 自增要求每条会改变关联的路径都记得配反向操作，漏一处就永久对不上。
    return True


async def _upsert_tags(session: AsyncSession, media: Media, item: ExtractedItem) -> int:
    """建立作品 ↔ 标签关联。返回新建的关联数。"""
    linked = 0
    for kind, name in item.tags:
        key = tag_norm_key(name)
        if not key:
            continue

        tag = await session.scalar(select(Tag).where(Tag.kind == kind, Tag.norm_key == key))
        if tag is None:
            tag = Tag(kind=TagKind(kind), name=name, norm_key=key)
            session.add(tag)
            await session.flush()

        exists = await session.scalar(
            select(media_tag.c.tag_id).where(
                media_tag.c.media_id == media.id, media_tag.c.tag_id == tag.id
            )
        )
        if exists is not None:
            continue
        await session.execute(
            media_tag.insert().values(media_id=media.id, tag_id=tag.id, created_at=utcnow())
        )
        tag.media_count += 1
        linked += 1
    return linked


async def _persist(
    session: AsyncSession, doc: RawDocument, outcome: ExtractionOutcome, report: ParseReport
) -> None:
    touched: set[int] = set()
    for item in outcome.items:
        media, created = await _upsert_media(session, item)
        report.media_created += int(created)
        report.media_reused += int(not created)
        report.tags_linked += await _upsert_tags(session, media, item)
        touched.add(media.id)
        for link in item.links:
            resource, is_new = await _upsert_resource(session, link, doc=doc, item=item)
            report.resources_created += int(is_new)
            report.resources_updated += int(not is_new)
            # 一个链接可以关联多部作品（合集），关联表的唯一约束保证不重复
            report.links_created += int(await _link_media_resource(session, media, resource))

    # 没归属到作品的链接照样入库（无任何关联），进人工/二次归属队列，绝不丢弃
    for link in outcome.unattributed_links:
        _, is_new = await _upsert_resource(session, link, doc=doc, item=None)
        report.resources_created += int(is_new)
        report.resources_updated += int(not is_new)
    report.unattributed_links = len(outcome.unattributed_links)

    # 本次碰过的作品，按关联表把两个冗余计数拉回一致
    await session.flush()
    await refresh_media_counters(session, touched)


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
                await session.flush()

            report.is_catalog = outcome.is_catalog
            await _persist(session, doc, outcome, report)

        # 目录帖不代表一部作品，标为 skipped 而非 done —— 让它在统计里可区分
        doc.parse_status = ParseStatus.SKIPPED if report.is_catalog else ParseStatus.DONE
        doc.parse_error = None
        doc.lease_until = None
        doc.next_parse_at = None
        report.status = doc.parse_status

    except IntegrityError:
        # 并发时另一个协程/进程抢先建了同一个 (norm_key, media_type, year) /
        # (provider, share_id) / (kind, norm_key)——不是这条文档本身有问题，
        # SAVEPOINT 已自动回滚，不计入 parse_attempts，留到下次自然重试。
        report.status = doc.parse_status
        report.error = "并发写入冲突，已回滚，留待下次重试（不计入失败次数）"
        logger.info("解析撞车 doc=%s: 并发写入冲突，留待下次重试", doc.id)

    except Exception as exc:
        doc.parse_attempts += 1
        doc.parse_error = f"{type(exc).__name__}: {exc}"
        doc.lease_until = None
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
