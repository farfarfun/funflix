from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from funflix.base.enums import SourceType
from funflix.models import RawDocument, Source
from funflix.services.collect.base import CollectedMessage, FetchResult
from funflix.services.collect.runner import collect_source


class StubCollector:
    """可控的采集器替身：runner 的职责是落库/去重/推水位，与 HTTP 无关。"""

    name = "stub"

    def __init__(self, result: FetchResult | None = None, error: Exception | None = None) -> None:
        self._result = result or FetchResult()
        self._error = error
        self.calls = 0

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        return "stub"

    async def fetch(self, source: Source) -> FetchResult:
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


def _msg(msg_id: int, text: str) -> CollectedMessage:
    return CollectedMessage(
        message_id=str(msg_id),
        text=text,
        published_at=datetime(2026, 8, 25, 7, 0, tzinfo=UTC),
        url=f"https://t.me/Chan/{msg_id}",
    )


async def _make_source(session, cursor: str | None = None) -> Source:
    source = Source(
        source_type=SourceType.TELEGRAM,
        url="https://t.me/s/Chan",
        identifier="Chan",
        cursor_message_id=cursor,
    )
    session.add(source)
    await session.flush()
    return source


@pytest.mark.asyncio
class TestCollectSource:
    async def test_writes_messages_and_advances_cursor(self, session) -> None:
        source = await _make_source(session)
        collector = StubCollector(
            FetchResult(messages=[_msg(101, "剧集A\n链接1"), _msg(102, "剧集B\n链接2")])
        )

        report = await collect_source(session, source, collector)
        await session.commit()

        assert (report.created, report.duplicated, report.fetched) == (2, 0, 2)
        assert source.cursor_message_id == "102"
        assert source.total_collected == 2
        assert source.last_success_at is not None
        assert await session.scalar(select(func.count()).select_from(RawDocument)) == 2

    async def test_links_documents_back_to_source(self, session) -> None:
        source = await _make_source(session)
        await collect_source(
            session, source, StubCollector(FetchResult(messages=[_msg(1, "剧集A")]))
        )
        await session.commit()

        doc = await session.scalar(select(RawDocument))
        assert doc.source_id == source.id
        assert doc.source_msg_id == "1"
        assert doc.source_type is SourceType.TELEGRAM
        assert doc.source_url == "https://t.me/Chan/1"

    async def test_empty_messages_are_skipped_but_still_advance_cursor(self, session) -> None:
        """纯图片消息不落库，但必须推进水位，否则它会永久卡住采集。"""
        source = await _make_source(session)
        collector = StubCollector(FetchResult(messages=[_msg(101, "剧集A"), _msg(102, "   ")]))

        report = await collect_source(session, source, collector)
        await session.commit()

        assert (report.created, report.skipped_empty) == (1, 1)
        assert source.cursor_message_id == "102"
        # 空消息不该计入产出统计
        assert source.total_collected == 1

    async def test_repeated_content_is_deduplicated(self, session) -> None:
        source = await _make_source(session)
        collector = StubCollector(
            FetchResult(messages=[_msg(101, "同样的正文"), _msg(102, "同样的正文")])
        )

        report = await collect_source(session, source, collector)
        await session.commit()

        assert (report.created, report.duplicated) == (1, 1)
        assert await session.scalar(select(func.count()).select_from(RawDocument)) == 1

    async def test_second_run_with_same_messages_creates_nothing(self, session) -> None:
        source = await _make_source(session)
        collector = StubCollector(FetchResult(messages=[_msg(101, "剧集A")]))

        await collect_source(session, source, collector)
        await session.commit()
        second = await collect_source(session, source, collector)
        await session.commit()

        assert second.created == 0
        assert source.cursor_message_id == "101"

    async def test_cursor_never_moves_backwards(self, session) -> None:
        """采集器若因翻页返回了更旧的消息，水位不能被拉回去。"""
        source = await _make_source(session, cursor="200")
        collector = StubCollector(FetchResult(messages=[_msg(101, "旧消息")]))

        await collect_source(session, source, collector)
        await session.commit()

        assert source.cursor_message_id == "200"

    async def test_truncated_schedules_immediate_retry(self, session) -> None:
        source = await _make_source(session)
        collector = StubCollector(FetchResult(messages=[_msg(101, "剧集A")], truncated=True))

        report = await collect_source(session, source, collector)
        await session.commit()

        assert report.truncated is True
        # 还有历史没取完，下一轮应立刻排上而不是等一个完整周期
        assert source.next_fetch_at is not None
        assert (source.next_fetch_at - source.last_fetched_at).total_seconds() < 60


@pytest.mark.asyncio
class TestCollectFailure:
    async def test_failure_records_error_and_backs_off(self, session) -> None:
        source = await _make_source(session)
        collector = StubCollector(error=RuntimeError("页面改版了"))

        report = await collect_source(session, source, collector)
        await session.commit()

        assert report.ok is False
        assert "页面改版了" in report.error
        assert source.consecutive_failures == 1
        assert source.next_fetch_at > source.last_fetched_at
        assert source.cursor_message_id is None  # 失败不推水位

    async def test_backoff_grows_with_consecutive_failures(self, session) -> None:
        source = await _make_source(session)
        collector = StubCollector(error=RuntimeError("boom"))

        await collect_source(session, source, collector)
        first = source.next_fetch_at - source.last_fetched_at
        await collect_source(session, source, collector)
        second = source.next_fetch_at - source.last_fetched_at
        await session.commit()

        assert second > first
        assert source.consecutive_failures == 2

    async def test_success_resets_failure_counter(self, session) -> None:
        source = await _make_source(session)
        await collect_source(session, source, StubCollector(error=RuntimeError("boom")))
        assert source.consecutive_failures == 1

        await collect_source(session, source, StubCollector(FetchResult(messages=[_msg(1, "A")])))
        await session.commit()

        assert source.consecutive_failures == 0
        assert source.last_error is None

    async def test_missing_collector_is_reported_not_raised(self, session) -> None:
        source = Source(
            source_type=SourceType.WEIBO,  # 尚未实现采集器
            url="https://weibo.com/x",
            identifier="x",
        )
        session.add(source)
        await session.flush()

        report = await collect_source(session, source)
        await session.commit()

        assert report.ok is False
        assert "weibo" in report.error


@pytest.mark.asyncio
class TestBackfillCursorBootstrap:
    """低水位的初始化。加回溯功能前就已追平的源，是最容易被漏掉的一类。"""

    async def test_uses_oldest_message_when_fetch_returns_some(self, session) -> None:
        source = await _make_source(session)
        collector = StubCollector(FetchResult(messages=[_msg(105, "甲"), _msg(107, "乙")]))
        await collect_source(session, source, collector)
        await session.commit()

        assert source.backfill_cursor_id == "105"

    async def test_falls_back_to_high_watermark_when_no_new_messages(self, session) -> None:
        """已追平的源每轮都返回 0 条新消息。

        若只在"有新消息"时才立低水位，回溯永远不会启动 —— 这是实测踩到的 bug。
        """
        source = await _make_source(session, cursor="96911")
        await collect_source(session, source, StubCollector(FetchResult(messages=[])))
        await session.commit()

        assert source.backfill_cursor_id == "96911"

    async def test_does_not_overwrite_existing_backfill_cursor(self, session) -> None:
        source = await _make_source(session, cursor="500")
        source.backfill_cursor_id = "120"
        await collect_source(session, source, StubCollector(FetchResult(messages=[])))
        await session.commit()

        assert source.backfill_cursor_id == "120"


@pytest.mark.asyncio
class TestProgressHook:
    """采集途中的页级进度上报。

    采一个源可能翻上百页、跑好几分钟，而它对外只是「一个源」——
    没有页级回调就只能盯着一个不动的进度条，分不清是在正常翻页还是卡死了。
    """

    async def test_hook_fires_per_page(self, session) -> None:
        from funflix.services.collect.base import CollectProgress, SupportsProgress

        seen: list[CollectProgress] = []

        class _C(SupportsProgress):
            name = "fake"

            @staticmethod
            def normalize_identifier(url):
                return "demo"

            async def fetch(self, src):
                for page in (1, 2, 3):
                    self._report("fetch", page, 5, page * 20, position=1000 - page)
                return FetchResult(messages=[], truncated=False)

            async def backfill(self, src):
                self._report("backfill", 1, 5, 7)
                return FetchResult(backfill_done=True)

        source = await _make_source(session)
        await session.commit()

        await collect_source(session, source, _C(), on_progress=seen.append)

        stages = [p.stage for p in seen]
        assert stages == ["fetch", "fetch", "fetch", "backfill"]
        assert [p.pages for p in seen[:3]] == [1, 2, 3]
        assert seen[0].budget == 5
        assert seen[2].messages == 60
        assert seen[0].position == "999"

    async def test_no_hook_is_fine(self, session) -> None:
        """不关心进度的调用方（worker）不传回调，采集器不能因此报错。"""
        from funflix.services.collect.base import SupportsProgress

        class _C(SupportsProgress):
            name = "fake"

            @staticmethod
            def normalize_identifier(url):
                return "demo"

            async def fetch(self, src):
                self._report("fetch", 1, 1, 0)
                return FetchResult(messages=[])

            async def backfill(self, src):
                return FetchResult(backfill_done=True)

        source = await _make_source(session)
        await session.commit()
        report = await collect_source(session, source, _C())
        assert report.ok
