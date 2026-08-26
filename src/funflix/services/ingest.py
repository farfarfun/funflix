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

    同一批次内部也可能有重复文本，逐条走 `ingest_document` 天然处理：
    第一条插入后，后续同 hash 的会在 flush 后的查询里命中它。
    """
    outcomes: list[IngestOutcome] = []
    for payload in payloads:
        outcomes.append(await ingest_document(session, payload))
    return outcomes
