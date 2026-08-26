"""采集编排：拉取 → 落库 → 推进水位。

采集器只负责取数据，落库去重、水位推进、失败退避统一在这里，
这样新增采集源不需要重复实现这些容易出错的部分。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.backoff import MAX_BACKOFF, backoff
from funflix.models import Source, utcnow
from funflix.schemas.raw import RawDocumentCreate
from funflix.services.collect.base import CollectedMessage, Collector
from funflix.services.collect.registry import get_collector
from funflix.services.ingest import ingest_document

logger = logging.getLogger(__name__)

@dataclass(slots=True)
class CollectReport:
    source_id: int
    fetched: int = 0
    created: int = 0
    duplicated: int = 0
    skipped_empty: int = 0
    pages_fetched: int = 0
    truncated: bool = False
    cursor_before: str | None = None
    cursor_after: str | None = None
    # --- 反向补历史 ---
    backfilled: int = 0
    backfill_created: int = 0
    backfill_cursor: str | None = None
    backfill_done: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def collect_source(
    session: AsyncSession,
    source: Source,
    collector: Collector | None = None,
) -> CollectReport:
    """采集一个源，把新消息写成 RawDocument 并推进水位。"""
    report = CollectReport(source_id=source.id, cursor_before=source.cursor_message_id)
    collector = collector or get_collector(source.source_type)
    now = utcnow()
    source.last_fetched_at = now

    if collector is None:
        report.error = f"没有 {source.source_type.value} 类型的采集器"
        source.last_error = report.error
        source.next_fetch_at = now + MAX_BACKOFF
        return report

    try:
        result = await collector.fetch(source)
    except Exception as exc:  # 网络抖动、页面改版、被限流都收敛到这里
        source.consecutive_failures += 1
        source.last_error = f"{type(exc).__name__}: {exc}"
        source.next_fetch_at = now + backoff(source.consecutive_failures)
        report.error = source.last_error
        logger.warning("采集失败 source=%s: %s", source.identifier, source.last_error)
        return report

    report.fetched = len(result.messages)
    report.pages_fetched = result.pages_fetched
    report.truncated = result.truncated

    if result.title and not source.title:
        source.title = result.title

    if result.state:
        # 采集器自定义水位（如文档版本号）。合并而非覆盖，
        # 让采集器只上报本轮变化的部分即可。
        source.extra = {**source.extra, **result.state}

    created, duplicated, skipped = await _ingest_messages(session, source, result.messages)
    report.created += created
    report.duplicated += duplicated
    report.skipped_empty += skipped

    # 水位按「见到的最大消息 ID」推进，而不是「成功落库的最大 ID」——
    # 空消息和重复消息也算已处理，否则水位会被它们永久卡住。
    newest = max(
        (m.numeric_id for m in result.messages if m.numeric_id is not None),
        default=None,
    )
    if newest is not None:
        current = int(source.cursor_message_id) if _is_int(source.cursor_message_id) else None
        if current is None or newest > current:
            source.cursor_message_id = str(newest)
            newest_msg = next(m for m in result.messages if m.numeric_id == newest)
            source.cursor_published_at = newest_msg.published_at

    # 低水位的起点。没有它就无从往前回溯。
    #
    # 优先用本轮见到的最早一条；本轮没有新消息时**必须回落到高水位** ——
    # 否则已经追平的源永远等不到"有新消息"的那一轮，低水位立不起来，
    # 回溯从头到尾不会启动。（这正是加回溯功能前就已追平的源的处境。）
    if source.backfill_cursor_id is None:
        oldest = min(
            (m.numeric_id for m in result.messages if m.numeric_id is not None),
            default=None,
        )
        if oldest is not None:
            source.backfill_cursor_id = str(oldest)
        elif source.cursor_message_id:
            # 高水位那条是确定见过的，从它往前翻即可。
            # 途中会重新遇到已入库的消息，交给 content_hash 去重。
            source.backfill_cursor_id = source.cursor_message_id

    source.total_collected += report.created
    source.consecutive_failures = 0
    source.last_error = None
    source.last_success_at = now

    await _run_backfill(session, source, collector, report)

    # 还有未取完的内容就立刻排下一轮，别等一个完整周期
    pending_more = result.truncated or not source.backfill_done
    source.next_fetch_at = now + (
        timedelta(seconds=5) if pending_more else timedelta(seconds=source.fetch_interval_seconds)
    )
    report.cursor_after = source.cursor_message_id
    return report


async def _run_backfill(
    session: AsyncSession, source: Source, collector: Collector, report: CollectReport
) -> None:
    """往前补历史。

    与追新分开跑：追新每轮都做，补历史跑到拉不动为止就收工。
    补历史失败不影响本轮追新的成果 —— 所以异常在这里就地吞掉并记日志，
    不让它把已经成功的追新一起回滚。
    """
    if source.backfill_done:
        return

    try:
        result = await collector.backfill(source)
    except Exception as exc:
        logger.warning("补历史失败 source=%s: %s", source.identifier, exc)
        return

    report.backfilled = len(result.messages)
    report.pages_fetched += result.pages_fetched

    created, duplicated, skipped = await _ingest_messages(session, source, result.messages)
    report.backfill_created = created
    report.duplicated += duplicated
    report.skipped_empty += skipped

    if result.state:
        source.extra = {**source.extra, **result.state}
    if result.backfill_cursor is not None:
        source.backfill_cursor_id = result.backfill_cursor
    if result.backfill_done:
        source.backfill_done = True
        logger.info("source=%s 历史已补完", source.identifier)

    source.total_backfilled += created
    report.backfill_cursor = source.backfill_cursor_id
    report.backfill_done = source.backfill_done


async def _ingest_messages(
    session: AsyncSession, source: Source, messages: list[CollectedMessage]
) -> tuple[int, int, int]:
    """把消息落成原始文本。返回 (新增, 重复, 跳过的空消息)。"""
    created = duplicated = skipped = 0
    for message in messages:
        if not message.text.strip():
            # 纯图片/视频消息没有正文，跳过落库 —— 但水位照常推进，
            # 否则这类消息会卡住水位，每轮都被重新拉取。
            skipped += 1
            continue

        outcome = await ingest_document(
            session,
            RawDocumentCreate(
                content=message.text,
                source_id=source.id,
                source_type=source.source_type,
                source_name=source.title or source.identifier,
                source_url=message.url,
                source_msg_id=message.message_id,
                published_at=message.published_at,
            ),
        )
        if outcome.duplicated:
            duplicated += 1
        else:
            created += 1
    return created, duplicated, skipped


def _is_int(value: str | None) -> bool:
    return bool(value and value.isdigit())
