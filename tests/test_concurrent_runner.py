"""`concurrent_runner` 的 funworker 流水线：产出必须与顺序版 `parse_batch` 一致。

生产者/消费者线程各自建专属 `AsyncEngine`——`:memory:` 库每条连接都是空的，
必须落到共享文件的 SQLite 库，多个独立引擎才能看到同一份数据（见
`test_worker.py` 里 `two_sessions` 同样的理由）。
"""

from __future__ import annotations

import asyncio
import queue
import time
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from funflix.base.config import Settings
from funflix.base.enums import ParseStatus, SourceType
from funflix.models import Base, Media, RawDocument, Resource, utcnow
from funflix.services.extract.concurrent_runner import (
    _ParseConsumer,
    _pipeline_counts,
    _pipeline_pending,
    count_pending,
    run_parse_pipeline,
)
from funflix.services.extract.rule import RuleExtractor


def make_doc(n: int, **kwargs) -> RawDocument:
    defaults = dict(
        content=f"名称：并发剧集{n}\n链接：https://pan.quark.cn/s/fake{n:06d}",
        content_hash=f"hash{n:060d}",
        source_type=SourceType.MANUAL,
        collected_at=utcnow(),
        parse_status=ParseStatus.PENDING,
        extra={},
    )
    return RawDocument(**{**defaults, **kwargs})


@asynccontextmanager
async def open_session(url: str):
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def db_url(tmp_path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path}/pipeline.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return url


class TestRunParsePipeline:
    @pytest.mark.asyncio
    async def test_matches_sequential_parse_batch_for_independent_titles(self, db_url) -> None:
        async with open_session(db_url) as session:
            session.add_all([make_doc(n) for n in range(1, 4)])
            await session.commit()

        reports = run_parse_pipeline(
            extractor_name="rule", settings=Settings(database_url=db_url), concurrency=1
        )

        assert len(reports) == 3
        assert all(r.ok for r in reports)
        async with open_session(db_url) as session:
            docs = list(await session.scalars(select(RawDocument)))
            assert {d.parse_status for d in docs} == {ParseStatus.DONE}
            media_rows = list(await session.scalars(select(Media)))
            assert len(media_rows) == 3

    @pytest.mark.asyncio
    async def test_concurrency_and_multiple_flushes_still_dedupe_shared_media(self, db_url) -> None:
        """3 线程处理单元 + 每 2 条落库一次：跨多次 flush 的作品去重要靠查库命中，
        不能只靠单次批内的内存 `BatchCache`。"""
        async with open_session(db_url) as session:
            docs = [
                make_doc(n, content=f"名称：热门剧\n链接：https://pan.quark.cn/s/fake{n:06d}")
                for n in range(1, 7)
            ]
            session.add_all(docs)
            await session.commit()

        reports = run_parse_pipeline(
            extractor_name="rule",
            settings=Settings(database_url=db_url),
            concurrency=3,
            batch_size=2,
            write_batch=2,
        )

        assert len(reports) == 6
        assert all(r.ok for r in reports)
        async with open_session(db_url) as session:
            media_rows = list(await session.scalars(select(Media)))
            assert len(media_rows) == 1, "同一部作品被多次 flush 重复建了"
            resource_rows = list(await session.scalars(select(Resource)))
            assert len(resource_rows) == 6

    @pytest.mark.asyncio
    async def test_cache_hit_skips_extract_and_reports_from_cache(
        self, db_url, monkeypatch
    ) -> None:
        async with open_session(db_url) as session:
            doc = make_doc(1)
            session.add(doc)
            await session.commit()
            doc_id = doc.id

        settings = Settings(database_url=db_url)
        first = run_parse_pipeline(extractor_name="rule", settings=settings, concurrency=1)
        assert len(first) == 1
        assert first[0].ok
        assert first[0].from_cache is False

        async with open_session(db_url) as session:
            doc = await session.get(RawDocument, doc_id)
            doc.parse_status = ParseStatus.PENDING
            doc.next_parse_at = None
            await session.commit()

        def _boom(self, content):  # noqa: ANN001
            raise AssertionError("缓存命中时不该调用 extract()")

        monkeypatch.setattr(RuleExtractor, "extract", _boom)

        second = run_parse_pipeline(extractor_name="rule", settings=settings, concurrency=1)

        assert len(second) == 1
        assert second[0].ok, second[0].error
        assert second[0].from_cache is True

    @pytest.mark.asyncio
    async def test_extract_failure_is_forwarded_to_backoff_not_lost(
        self, db_url, monkeypatch
    ) -> None:
        """处理单元里 `extract()` 抛异常不能被 funworker 默认语义悄悄吞掉——
        必须原样传给消费者，落到跟 `parse_document` 一样的失败退避分支。"""
        async with open_session(db_url) as session:
            doc = make_doc(1)
            session.add(doc)
            await session.commit()
            doc_id = doc.id

        async def _boom(self, content):  # noqa: ANN001
            raise RuntimeError("boom")

        monkeypatch.setattr(RuleExtractor, "extract", _boom)

        reports = run_parse_pipeline(
            extractor_name="rule", settings=Settings(database_url=db_url), concurrency=1
        )

        assert len(reports) == 1
        assert reports[0].ok is False
        assert "boom" in (reports[0].error or "")

        async with open_session(db_url) as session:
            doc = await session.get(RawDocument, doc_id)
            assert doc.parse_attempts == 1
            assert doc.parse_status is ParseStatus.PENDING
            assert doc.next_parse_at is not None
            assert doc.next_parse_at > utcnow()

    @pytest.mark.asyncio
    async def test_limit_caps_how_many_documents_are_processed(self, db_url) -> None:
        async with open_session(db_url) as session:
            session.add_all([make_doc(n) for n in range(1, 6)])
            await session.commit()

        reports = run_parse_pipeline(
            extractor_name="rule",
            settings=Settings(database_url=db_url),
            concurrency=2,
            limit=2,
        )

        assert len(reports) == 2

    @pytest.mark.asyncio
    async def test_empty_queue_returns_no_reports(self, db_url) -> None:
        reports = run_parse_pipeline(
            extractor_name="rule", settings=Settings(database_url=db_url), concurrency=2
        )
        assert reports == []

    @pytest.mark.asyncio
    async def test_progress_callback_reports_enqueued_and_done_counts(self, db_url) -> None:
        """`on_progress(total_enqueued, total_done)` 轮询驱动，不绑死在落库批大小上。"""
        async with open_session(db_url) as session:
            session.add_all([make_doc(n) for n in range(1, 6)])
            await session.commit()

        calls: list[tuple[int, int]] = []
        reports = run_parse_pipeline(
            extractor_name="rule",
            settings=Settings(database_url=db_url),
            concurrency=1,
            write_batch=100,
            on_progress=lambda total, done: calls.append((total, done)),
        )

        assert len(reports) == 5
        assert calls, "轮询进度回调至少要触发一次"
        # 最后一次回调时，入队/处理总数要对得上——流水线已经彻底跑空。
        total, done = calls[-1]
        assert total == done == 5


class TestPipelinePending:
    def test_pending_sums_both_queue_backlogs(self) -> None:
        class _FakeProducer:
            def stats(self) -> dict[str, int]:
                return {"output_qsize": 3}

        class _FakeConsumer:
            def stats(self) -> dict[str, int]:
                return {"input_qsize": 2}

        class _FakePipeline:
            producer = _FakeProducer()
            consumer = _FakeConsumer()

        assert _pipeline_pending(_FakePipeline()) == 5  # type: ignore[arg-type]

    def test_pending_zero_once_both_queues_drain(self) -> None:
        """两条队列都空了，即便消费者的提交数还没追上入队总数（还攒在内部
        缓冲区里没到 `write_batch`），也该判定为"可以收尾了"——不然循环会
        一直等一个永远不会自然发生的"提交数追上总数"，卡死在这里。"""

        class _FakeProducer:
            def stats(self) -> dict[str, int]:
                return {"output_qsize": 0}

        class _FakeConsumer:
            def stats(self) -> dict[str, int]:
                return {"input_qsize": 0}

        class _FakePipeline:
            producer = _FakeProducer()
            consumer = _FakeConsumer()

        assert _pipeline_pending(_FakePipeline()) == 0  # type: ignore[arg-type]


class TestPipelineCounts:
    def test_done_tracks_consumer_committed_not_pool_processed(self) -> None:
        """完成数要跟消费者真正提交到数据库的计数走，不能跟处理单元线程池的
        `processed` 走——后者只反映"内存里处理完了"，写库比抽取慢时会让进度条
        冲到 100% 后卡住一大截，看着像"瞬间跑完但数据库没写完"。"""

        class _FakeProducer:
            def stats(self) -> dict[str, int]:
                return {"produced": 150}

        class _FakePool:
            def stats(self) -> dict[str, int]:
                # 处理单元早就跑完了，但这不代表落库跟上了。
                return {"processed": 140, "failed": 3}

        class _FakeConsumer:
            def stats(self) -> dict[str, int]:
                return {"consumed": 40, "failed": 0, "committed": 25}

        class _FakePipeline:
            producer = _FakeProducer()
            pool = _FakePool()
            consumer = _FakeConsumer()

        total, done = _pipeline_counts(_FakePipeline())  # type: ignore[arg-type]

        assert total == 150
        assert done == 25


class TestParseConsumerFlushInterval:
    def test_flushes_on_time_even_when_under_write_batch(self, monkeypatch) -> None:
        """缓冲区还没攒够 write_batch，但过了 flush_interval，也要落库——
        不然处理单元比落库快时，最新一批文档会一直卡在消费者的缓冲区里出不去。"""
        consumer = _ParseConsumer(
            queue.Queue(),
            settings=Settings(database_url="sqlite+aiosqlite:///:memory:"),
            write_batch=1000,
            flush_interval=0.01,
        )
        consumer._aio_loop = asyncio.new_event_loop()
        consumer._buffer = []
        consumer._last_flush_at = time.monotonic()

        flushed: list[list[dict]] = []

        async def _fake_flush() -> None:
            flushed.append(list(consumer._buffer))
            consumer._buffer = []

        monkeypatch.setattr(consumer, "_flush", _fake_flush)

        consumer.consume({"doc_id": 1})
        assert not flushed, "刚开始不该立刻触发落库"

        time.sleep(0.02)
        consumer.consume({"doc_id": 2})
        assert flushed == [[{"doc_id": 1}, {"doc_id": 2}]]


class TestCountPending:
    @pytest.mark.asyncio
    async def test_counts_only_due_pending_documents(self, session) -> None:
        from datetime import timedelta

        session.add(make_doc(1))
        session.add(make_doc(2, parse_status=ParseStatus.DONE))
        session.add(make_doc(3, next_parse_at=utcnow() + timedelta(hours=1)))
        await session.commit()

        assert await count_pending(session, limit=None) == 1

    @pytest.mark.asyncio
    async def test_limit_caps_the_count(self, session) -> None:
        session.add_all([make_doc(n) for n in range(1, 6)])
        await session.commit()

        assert await count_pending(session, limit=2) == 2
