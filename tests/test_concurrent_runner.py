"""`concurrent_runner` 的 funworker 流水线：产出必须与顺序版 `parse_batch` 一致。

生产者/消费者线程各自建专属 `AsyncEngine`——`:memory:` 库每条连接都是空的，
必须落到共享文件的 SQLite 库，多个独立引擎才能看到同一份数据（见
`test_worker.py` 里 `two_sessions` 同样的理由）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from funflix.base.config import Settings
from funflix.base.enums import ParseStatus, SourceType
from funflix.models import Base, Media, RawDocument, Resource, utcnow
from funflix.services.extract.concurrent_runner import count_pending, run_parse_pipeline
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
