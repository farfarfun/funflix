"""`concurrent_runner` 的 funworker 流水线：产出必须与顺序版 `parse_batch` 一致。

生产者/消费者线程各自建专属 `AsyncEngine`——`:memory:` 库每条连接都是空的，
必须落到共享文件的 SQLite 库，多个独立引擎才能看到同一份数据（见
`test_worker.py` 里 `two_sessions` 同样的理由）。
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from funflix.base.config import Settings
from funflix.base.enums import ParseStatus, SourceType
from funflix.models import Base, Media, RawDocument, Resource, utcnow
from funflix.services.extract.concurrent_runner import (
    _HEX_DIGITS,
    _ParseConsumer,
    _pipeline_counts,
    _pipeline_pending,
    count_pending,
    parse_shard,
    run_parse_pipeline,
    shard_digits,
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
    async def test_sharded_runs_cover_every_document_exactly_once(self, db_url) -> None:
        """4 片依次跑完，等价于不分片跑一遍：每条文档恰好被处理一次。

        这是 `--shard` 存在的唯一理由，也是它唯一的风险点。生产环境 4 片是
        并行跑的，这里串行跑——要验的是**谓词把队列切干净了**，不是线程安全
        （各片进程之间除了数据库没有共享状态）。

        用 `len(reports)` 而不是只看状态：重复解析同一条文档也会让它停在 DONE，
        只查状态看不出来，报告条数才会露出多出来的那一次。
        """
        total_docs, shards = 24, 4
        async with open_session(db_url) as session:
            session.add_all([make_doc(n) for n in range(1, total_docs + 1)])
            await session.commit()

        settings = Settings(database_url=db_url)
        processed: list[object] = []
        for i in range(shards):
            reports = run_parse_pipeline(
                extractor_name="rule", settings=settings, concurrency=1, shard=(i, shards)
            )
            assert all(r.ok for r in reports), [r.error for r in reports if not r.ok]
            processed.extend(r.document_id for r in reports)

        assert len(processed) == total_docs, "有文档被漏掉或被重复解析了"
        assert len(set(processed)) == total_docs, "同一条文档出现在多个分片里"
        async with open_session(db_url) as session:
            docs = list(await session.scalars(select(RawDocument)))
            assert {d.parse_status for d in docs} == {ParseStatus.DONE}
            assert len(list(await session.scalars(select(Media)))) == total_docs

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
    def test_done_tracks_consumer_consumed_not_pool_processed(self) -> None:
        """完成数要跟消费者（`BaseBatchConsumer`）真正提交到数据库的计数走，不能
        跟处理单元线程池的 `processed` 走——后者只反映"内存里处理完了"，写库比
        抽取慢时会让进度条冲到 100% 后卡住一大截，看着像"瞬间跑完但数据库没写完"。
        """

        class _FakeProducer:
            def stats(self) -> dict[str, int]:
                return {"produced": 150}

        class _FakePool:
            def stats(self) -> dict[str, int]:
                # 处理单元早就跑完了，但这不代表落库跟上了。
                return {"processed": 140, "failed": 3}

        class _FakeConsumer:
            def stats(self) -> dict[str, int]:
                # BaseBatchConsumer 只在 consume_batch 跑完之后才按整批累加
                # consumed，语义上就是"落库成功的条目数"，跟 pool 的 processed
                # 不是一回事。
                return {"consumed": 25, "failed": 0}

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
        不然处理单元比落库快时，最新一批文档会一直卡在消费者的缓冲区里出不去。
        `_ParseConsumer` 是 `funworker.BaseBatchConsumer` 的子类，攒批/计时
        轮询逻辑都在基类的 `_loop()` 里，这里直接驱动真实的 `_loop()` 验证
        `flush_interval`（映射成基类的 `batch_timeout`）确实靠轮询触发，不
        依赖"下一条数据到达才检查"。"""
        q: queue.Queue = queue.Queue()
        consumer = _ParseConsumer(
            q,
            settings=Settings(database_url="sqlite+aiosqlite:///:memory:"),
            write_batch=1000,
            flush_interval=0.01,
        )
        consumer.get_timeout = 0.01

        flushed: list[list[dict]] = []
        monkeypatch.setattr(consumer, "consume_batch", flushed.append)

        thread = threading.Thread(target=consumer._loop)
        thread.start()
        try:
            q.put({"doc_id": 1})
            time.sleep(0.05)
            assert flushed == [[{"doc_id": 1}]], "过了 flush_interval 应该已经落库一次"

            q.put({"doc_id": 2})
            time.sleep(0.05)
        finally:
            consumer.request_stop()
            thread.join(timeout=2)

        assert flushed == [[{"doc_id": 1}], [{"doc_id": 2}]]


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


class TestParseShard:
    """`--shard i/N` 的解析与校验。"""

    def test_parses_index_and_total(self) -> None:
        assert parse_shard("0/8") == (0, 8)
        assert parse_shard("7/8") == (7, 8)

    @pytest.mark.parametrize(
        "spec",
        [
            "3",  # 没有斜杠
            "a/8",  # 序号不是整数
            "0/x",  # 总数不是整数
            "0/0",  # 总片数必须是正数
            "0/-2",
            "8/8",  # 序号从 0 开始，上界是开区间
            "9/8",
            "-1/8",
        ],
    )
    def test_rejects_malformed_or_out_of_range(self, spec: str) -> None:
        with pytest.raises(ValueError):
            parse_shard(spec)


class TestShardDigits:
    """分片必须既不重也不漏——这是多进程并行不重复解析的全部依据。"""

    @pytest.mark.parametrize("total", list(range(1, 17)))
    def test_every_digit_belongs_to_exactly_one_shard(self, total: int) -> None:
        buckets = [shard_digits(i, total) for i in range(total)]
        union: list[str] = [d for bucket in buckets for d in bucket]

        assert sorted(union) == sorted(_HEX_DIGITS), f"{total} 片没有覆盖全部十六进制字符"
        assert len(union) == len(set(union)), f"{total} 片之间有重叠"

    @pytest.mark.parametrize("total", [1, 2, 4, 8, 16])
    def test_divisors_of_sixteen_split_evenly(self, total: int) -> None:
        """16 的因数要严格等量；其余片数允许差一个字符，不额外约束。"""
        sizes = {len(shard_digits(i, total)) for i in range(total)}

        assert sizes == {16 // total}


class TestCountPendingSharded:
    """分片谓词得在真库上成立。

    谓词是 `lower(substr(id::text, length(id::text), 1))`，依赖主键在该方言下
    的**文本形态**：PG 上是原生 uuid 转文本（带连字符），SQLite 上是
    `sa.Uuid` 存的 32 位十六进制串。两边末位字符都是 uuid 的最后一位十六进制数，
    所以谓词可移植——但这件事只能在真库上跑一遍才算证明，纯单元测试覆盖不到。
    """

    @pytest.mark.asyncio
    async def test_shards_partition_the_pending_queue(self, session) -> None:
        total_docs = 64
        session.add_all([make_doc(n) for n in range(1, total_docs + 1)])
        await session.commit()

        assert await count_pending(session, limit=None) == total_docs

        counts = [await count_pending(session, limit=None, shard=(i, 4)) for i in range(4)]

        assert sum(counts) == total_docs, f"4 片加起来漏了或重了：{counts}"
        assert all(c > 0 for c in counts), f"uuid7 末位随机，64 条不该有空片：{counts}"

    @pytest.mark.asyncio
    async def test_single_shard_is_the_whole_queue(self, session) -> None:
        """`N=1` 等价于不分片——谓词应当被整个省掉，而不是退化成某一片。"""
        session.add_all([make_doc(n) for n in range(1, 11)])
        await session.commit()

        assert await count_pending(session, limit=None, shard=(0, 1)) == 10

    @pytest.mark.asyncio
    async def test_shard_respects_the_other_pending_conditions(self, session) -> None:
        """分片只是**再加**一个谓词，不能放宽状态/退避这两道原有条件。"""
        from datetime import timedelta

        session.add_all([make_doc(n, parse_status=ParseStatus.DONE) for n in range(1, 21)])
        session.add_all(
            [make_doc(n, next_parse_at=utcnow() + timedelta(hours=1)) for n in range(21, 41)]
        )
        await session.commit()

        counts = [await count_pending(session, limit=None, shard=(i, 4)) for i in range(4)]

        assert sum(counts) == 0, f"已完成和未到期的文档被分片放进来了：{counts}"
