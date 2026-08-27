"""摄入服务：原始文本落库 + 入口去重。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import ParseStatus
from funflix.models import RawDocument, utcnow
from funflix.schemas.raw import RawDocumentCreate

logger = logging.getLogger(__name__)


def normalize_for_hash(content: str) -> str:
    """计算指纹前的规范化。

    只抹掉不影响语义的排版噪声（行尾空白、空行、首尾空白），
    保留换行结构 —— 换行是"一条文本里有多部作品"的重要边界信号，
    全部压成空格会让两条结构不同的文本撞成同一个 hash。
    """
    lines = (line.strip() for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


def content_hash(content: str) -> str:
    return hashlib.sha256(normalize_for_hash(content).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class IngestOutcome:
    document: RawDocument
    duplicated: bool


async def ingest_document(
    session: AsyncSession,
    payload: RawDocumentCreate,
) -> IngestOutcome:
    """写入一条原始文本；已存在则原样返回已有记录。

    去重是幂等性的第一道闸：同一条分享被多个采集器重复抓到时，
    这里挡住即可，不会走到后面按次计费的 LLM 抽取。
    """
    digest = content_hash(payload.content)

    existing = await session.scalar(select(RawDocument).where(RawDocument.content_hash == digest))
    if existing is not None:
        return IngestOutcome(document=existing, duplicated=True)

    now = utcnow()
    doc = RawDocument(
        content=payload.content,
        content_hash=digest,
        source_id=payload.source_id,
        source_type=payload.source_type,
        source_name=payload.source_name,
        source_url=payload.source_url,
        source_msg_id=payload.source_msg_id,
        published_at=payload.published_at,
        collected_at=now,
        extra=payload.extra,
        parse_status=ParseStatus.PENDING,
        next_parse_at=now,
    )
    session.add(doc)

    try:
        # 用 savepoint 包住：并发下另一个请求可能刚插入同一 hash，
        # 触发唯一约束时只回滚这一条，不牵连同批次的其他文档。
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        logger.info("并发插入命中 content_hash 冲突，回落到已有记录: %s", digest)
        session.expunge(doc)
        winner = await session.scalar(select(RawDocument).where(RawDocument.content_hash == digest))
        if winner is None:  # pragma: no cover - 理论上不可达
            raise
        return IngestOutcome(document=winner, duplicated=True)

    return IngestOutcome(document=doc, duplicated=False)


async def ingest_many(
    session: AsyncSession,
    payloads: list[RawDocumentCreate],
) -> list[IngestOutcome]:
    """批量摄入。

    去重按整批一次 `IN` 查询预读已有 content_hash，而不是像 `ingest_document`
    那样每条一次 SELECT + flush 各一次往返——远程数据库单次往返约
    ~100~150ms，一个批次可能有成百上千条消息，逐条查询会让写入耗时线性
    膨胀，拖慢采集翻页（见 `services/collect/runner.py` 模块 docstring）。
    批内出现的重复 hash 也在这里就地命中，不用等各自 flush 后再去查库撞见。
    """
    if not payloads:
        return []

    digests = [content_hash(p.content) for p in payloads]
    existing_rows = await session.scalars(
        select(RawDocument).where(RawDocument.content_hash.in_(set(digests)))
    )
    by_hash: dict[str, RawDocument] = {doc.content_hash: doc for doc in existing_rows}

    now = utcnow()
    outcomes: list[IngestOutcome] = []
    fresh_indices: list[int] = []
    fresh_docs: list[RawDocument] = []

    for i, (payload, digest) in enumerate(zip(payloads, digests, strict=True)):
        existing_doc = by_hash.get(digest)
        if existing_doc is not None:
            outcomes.append(IngestOutcome(document=existing_doc, duplicated=True))
            continue

        doc = RawDocument(
            content=payload.content,
            content_hash=digest,
            source_id=payload.source_id,
            source_type=payload.source_type,
            source_name=payload.source_name,
            source_url=payload.source_url,
            source_msg_id=payload.source_msg_id,
            published_at=payload.published_at,
            collected_at=now,
            extra=payload.extra,
            parse_status=ParseStatus.PENDING,
            next_parse_at=now,
        )
        session.add(doc)
        by_hash[digest] = doc  # 批内去重：后续同 hash 直接命中这条，不重复新建
        fresh_indices.append(i)
        fresh_docs.append(doc)
        outcomes.append(IngestOutcome(document=doc, duplicated=False))

    if fresh_docs:
        try:
            # 用 savepoint 包住：整批一起 flush 期间撞上并发写入的概率很低，
            # 一旦撞上就退化到逐条处理，不让整批因为一条冲突同归于尽。
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            logger.info("批量插入命中 content_hash 冲突，退化为逐条处理: %d 条", len(fresh_docs))
            for doc in fresh_docs:
                session.expunge(doc)
            for i in fresh_indices:
                outcomes[i] = await ingest_document(session, payloads[i])

    return outcomes
