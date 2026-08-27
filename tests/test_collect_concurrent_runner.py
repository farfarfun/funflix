"""`collect` 的 funworker 流水线：产出必须与顺序版 `collect_source` 一致，
且 Telegram 补历史的乐观水位推进要真的先于任务处理落库。

生产者/消费者线程各自建专属 `AsyncEngine`——`:memory:` 库每条连接都是空的，
必须落到共享文件的 SQLite 库，多个独立引擎才能看到同一份数据（见
`test_concurrent_runner.py`（parse）同样的理由）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from queue import Queue

import pytest
from factories import build_page, simple_page
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from funflix.base.config import Settings
from funflix.base.enums import SourceType
from funflix.models import Base, RawDocument, Source
from funflix.services.collect import concurrent_runner as cr
from funflix.services.collect import telegram
from funflix.services.collect.base import CollectedMessage, FetchResult
from funflix.services.collect.concurrent_runner import (
    BackfillPageTotals,
    _CollectProducer,
    _TelegramPageJob,
    run_collect_pipeline,
)
from funflix.services.collect.registry import get_collector_class as _real_get_collector_class

CHANNEL = "Chan"


@asynccontextmanager
async def open_session(url: str):
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def db_url(tmp_path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path}/collect.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return url


def _msg(msg_id: int, text: str) -> CollectedMessage:
    return CollectedMessage(
        message_id=str(msg_id),
        text=text,
        published_at=datetime(2026, 8, 25, 7, 0, tzinfo=UTC),
        url=f"https://stub.example/{msg_id}",
    )


def _source(identifier: str, source_type: SourceType = SourceType.TENCENT_DOC, **kwargs) -> Source:
    return Source(
        source_type=source_type,
        url=f"https://stub.example/{identifier}",
        identifier=identifier,
        **kwargs,
    )


# --- 非 Telegram 源当作不透明整源任务处理，靠一个可控的替身采集器验证 ---

_STUB_RESULTS: dict[str, FetchResult] = {}
_STUB_ERRORS: dict[str, Exception] = {}


class StubCollector:
    """整源任务的替身：只关心 runner 落库/去重/推水位，与 HTTP 无关。"""

    name = "stub"

    def __init__(self, client=None) -> None:  # noqa: ANN001
        self.client = client

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        return "stub"

    async def fetch(self, source: Source) -> FetchResult:
        if source.identifier in _STUB_ERRORS:
            raise _STUB_ERRORS[source.identifier]
        return _STUB_RESULTS.get(source.identifier, FetchResult())

    async def backfill(self, source: Source) -> FetchResult:
        return FetchResult(backfill_done=True)


@pytest.fixture(autouse=True)
def _stub_registry(monkeypatch):
    """把 `SourceType.TENCENT_DOC` 接到 `StubCollector`，其余类型走真实注册表。"""
    _STUB_RESULTS.clear()
    _STUB_ERRORS.clear()

    def fake_get_collector_class(source_type: SourceType):
        if source_type == SourceType.TENCENT_DOC:
            return StubCollector
        return _real_get_collector_class(source_type)

    monkeypatch.setattr(cr, "get_collector_class", fake_get_collector_class)
    yield
    _STUB_RESULTS.clear()
    _STUB_ERRORS.clear()


@pytest.mark.asyncio
class TestOpaqueSources:
    async def test_matches_sequential_collect_source_for_single_source(self, db_url) -> None:
        async with open_session(db_url) as session:
            source = _source("s1")
            session.add(source)
            await session.commit()
            source_id = source.id

        _STUB_RESULTS["s1"] = FetchResult(messages=[_msg(101, "剧集A"), _msg(102, "剧集B")])

        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=1)

        assert len(result.reports) == 1
        identifier, report = result.reports[0]
        assert identifier == "s1"
        assert (report.created, report.duplicated, report.fetched) == (2, 0, 2)
        assert report.cursor_after == "102"

        async with open_session(db_url) as session:
            refreshed = await session.get(Source, source_id)
            assert refreshed.cursor_message_id == "102"
            assert refreshed.total_collected == 2
            assert await session.scalar(select(func.count()).select_from(RawDocument)) == 2

    async def test_multiple_sources_share_one_queue(self, db_url) -> None:
        async with open_session(db_url) as session:
            session.add_all([_source("s1"), _source("s2"), _source("s3")])
            await session.commit()

        _STUB_RESULTS["s1"] = FetchResult(messages=[_msg(1, "甲")])
        _STUB_RESULTS["s2"] = FetchResult(messages=[_msg(1, "乙"), _msg(2, "丙")])
        _STUB_RESULTS["s3"] = FetchResult(messages=[])

        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=3)

        assert len(result.reports) == 3
        by_identifier = dict(result.reports)
        assert by_identifier["s1"].created == 1
        assert by_identifier["s2"].created == 2
        assert by_identifier["s3"].created == 0

        async with open_session(db_url) as session:
            assert await session.scalar(select(func.count()).select_from(RawDocument)) == 3

    async def test_content_hash_dedup_across_concurrent_sources(self, db_url) -> None:
        """两个不同源抓到同样正文的消息，只落一条——去重键是全局的，不分源。"""
        async with open_session(db_url) as session:
            session.add_all([_source("s1"), _source("s2")])
            await session.commit()

        _STUB_RESULTS["s1"] = FetchResult(messages=[_msg(1, "同样的正文")])
        _STUB_RESULTS["s2"] = FetchResult(messages=[_msg(1, "同样的正文")])

        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=2)

        by_identifier = dict(result.reports)
        assert by_identifier["s1"].created + by_identifier["s2"].created == 1
        assert by_identifier["s1"].duplicated + by_identifier["s2"].duplicated == 1

        async with open_session(db_url) as session:
            assert await session.scalar(select(func.count()).select_from(RawDocument)) == 1

    async def test_fetch_failure_is_reported_not_raised(self, db_url) -> None:
        async with open_session(db_url) as session:
            source = _source("s1")
            session.add(source)
            await session.commit()

        _STUB_ERRORS["s1"] = RuntimeError("页面改版了")

        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=1)

        assert len(result.reports) == 1
        identifier, report = result.reports[0]
        assert identifier == "s1"
        assert report.ok is False
        assert "页面改版了" in report.error

    async def test_missing_collector_is_reported_not_raised(self, db_url) -> None:
        async with open_session(db_url) as session:
            source = _source("s1", source_type=SourceType.WEIBO)
            session.add(source)
            await session.commit()

        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=1)

        assert len(result.reports) == 1
        identifier, report = result.reports[0]
        assert identifier == "s1"
        assert report.ok is False
        assert "weibo" in report.error

    async def test_no_enabled_sources_returns_empty_result(self, db_url) -> None:
        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=2)

        assert result.reports == []
        assert result.backfill_pages == BackfillPageTotals()

    async def test_disabled_source_is_skipped(self, db_url) -> None:
        async with open_session(db_url) as session:
            session.add(_source("s1", enabled=False))
            await session.commit()

        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=1)

        assert result.reports == []

    async def test_progress_callback_fires(self, db_url) -> None:
        async with open_session(db_url) as session:
            session.add(_source("s1"))
            await session.commit()

        calls: list[str] = []
        run_collect_pipeline(
            settings=Settings(database_url=db_url), concurrency=1, on_progress=calls.append
        )

        assert calls, "队列计数进度回调至少要触发一次"


# --- Telegram 补历史：翻页解耦 + 乐观水位推进 ---


class TestTelegramOptimisticWatermarkAdvance:
    def test_backfill_cursor_committed_at_produce_time_before_any_job_is_processed(
        self, db_url
    ) -> None:
        """规划阶段就把新低水位落库、提交——不等这些页真的被处理单元消费。

        同步测试，不是 `async def`：`_CollectProducer` 自己在 `on_start()` 里
        起一个独立的事件循环，跟 pytest-asyncio 给测试协程用的那个循环不是
        同一个——在协程里再对另一个循环调 `run_until_complete` 会直接报
        "Cannot run the event loop while another loop is running"，跟生产者
        线程实际跑起来时（一个全新的 OS 线程，压根没有正在跑的循环）的情形
        不符，所以这里改用同步测试 + `asyncio.run` 驱动异步的准备/断言部分。
        """

        async def _setup() -> int:
            async with open_session(db_url) as session:
                source = Source(
                    source_type=SourceType.TELEGRAM,
                    url=f"https://t.me/s/{CHANNEL}",
                    identifier=CHANNEL,
                    backfill_cursor_id="45",
                )
                session.add(source)
                await session.commit()
                return source.id

        source_id = asyncio.run(_setup())

        producer = _CollectProducer(
            Queue(), settings=Settings(database_url=db_url), source_id=None, batch_size=500
        )
        producer.on_start()
        try:
            producer._aio_loop.run_until_complete(producer._fetch_page())
        finally:
            producer.on_stop()

        # 缓冲区里已经有规划好的翻页任务，但一个都还没被处理单元处理过
        page_jobs = [job for job in producer._buffer if isinstance(job, _TelegramPageJob)]
        assert [j.before for j in page_jobs] == [45, 25, 5]

        async def _check() -> Source:
            async with open_session(db_url) as session:
                return await session.get(Source, source_id)

        refreshed = asyncio.run(_check())
        assert refreshed.backfill_cursor_id == "1"
        assert refreshed.backfill_done is True


@pytest.mark.asyncio
class TestTelegramBackfillPipeline:
    async def test_decomposed_pages_are_fetched_concurrently_and_ingested(
        self, db_url, monkeypatch
    ) -> None:
        async with open_session(db_url) as session:
            source = Source(
                source_type=SourceType.TELEGRAM,
                url=f"https://t.me/s/{CHANNEL}",
                identifier=CHANNEL,
                backfill_cursor_id="45",
            )
            session.add(source)
            await session.commit()
            source_id = source.id

        pages = {
            45: simple_page(CHANNEL, [41, 42, 43]),
            25: simple_page(CHANNEL, [21, 22, 23]),
            5: simple_page(CHANNEL, [1, 2, 3]),
        }

        async def fake_fetch_page_html(client, channel, before):  # noqa: ANN001
            return pages.get(before, build_page(channel, []))

        # 追新（不透明整源任务）用的是 telegram.py 自己的模块级引用；
        # 拆页任务用的是 concurrent_runner.py import 进来的那一份——两处都要打。
        monkeypatch.setattr(telegram, "fetch_page_html", fake_fetch_page_html)
        monkeypatch.setattr(cr, "fetch_page_html", fake_fetch_page_html)

        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=3)

        assert result.backfill_pages.pages == 3
        assert result.backfill_pages.created == 9
        assert result.backfill_pages.duplicated == 0

        async with open_session(db_url) as session:
            count = await session.scalar(select(func.count()).select_from(RawDocument))
            assert count == 9
            refreshed = await session.get(Source, source_id)
            assert refreshed.backfill_done is True
            assert refreshed.backfill_cursor_id == "1"
            assert refreshed.total_backfilled == 9

    async def test_page_fetch_failure_yields_empty_page_not_a_crash(
        self, db_url, monkeypatch
    ) -> None:
        async with open_session(db_url) as session:
            session.add(
                Source(
                    source_type=SourceType.TELEGRAM,
                    url=f"https://t.me/s/{CHANNEL}",
                    identifier=CHANNEL,
                    backfill_cursor_id="25",
                )
            )
            await session.commit()

        async def boom(client, channel, before):  # noqa: ANN001
            raise RuntimeError("network blip")

        monkeypatch.setattr(telegram, "fetch_page_html", boom)
        monkeypatch.setattr(cr, "fetch_page_html", boom)

        result = run_collect_pipeline(settings=Settings(database_url=db_url), concurrency=2)

        assert result.backfill_pages.pages == 2
        assert result.backfill_pages.created == 0
