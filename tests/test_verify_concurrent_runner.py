"""`concurrent_runner` 的 funworker 流水线：产出必须与顺序版 `check_resource` 一致。

生产者/消费者线程各自建专属 `AsyncEngine`——`:memory:` 库每条连接都是空的，
必须落到共享文件的 SQLite 库，多个独立引擎才能看到同一份数据（见
`tests/test_concurrent_runner.py` 同样的理由）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from funflix.base.config import Settings
from funflix.base.enums import CheckStatus, Provider
from funflix.models import Base, Resource, utcnow
from funflix.services.verify import concurrent_runner as cr
from funflix.services.verify.base import CheckOutcome, LinkRef


class FakeProbe:
    name = "fake"
    needs_auth = False

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    async def check(self, ref: LinkRef) -> CheckOutcome:
        if ref.share_id.startswith("boom"):
            raise RuntimeError("probe exploded")
        status = CheckStatus.INVALID if ref.share_id.startswith("invalid") else CheckStatus.VALID
        return CheckOutcome(status=status, title=f"title-{ref.share_id}")


def fake_get_probe(provider: Provider):
    return FakeProbe(provider)


def make_resource(n: int, **kwargs) -> Resource:
    now = utcnow()
    defaults = dict(
        provider=Provider.QUARK,
        share_id=f"share{n:06d}",
        url=f"https://pan.quark.cn/s/fake{n:06d}",
        check_status=CheckStatus.UNCHECKED,
        first_seen_at=now,
        last_seen_at=now,
    )
    return Resource(**{**defaults, **kwargs})


@asynccontextmanager
async def open_session(url: str):
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def db_url(tmp_path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path}/verify_pipeline.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return url


class TestRunVerifyPipeline:
    @pytest.mark.asyncio
    async def test_matches_sequential_check_resource_for_independent_resources(
        self, db_url, monkeypatch
    ) -> None:
        monkeypatch.setattr(cr, "get_probe", fake_get_probe)
        async with open_session(db_url) as session:
            session.add_all([make_resource(n) for n in range(1, 4)])
            await session.commit()

        reports = cr.run_verify_pipeline(
            settings=Settings(database_url=db_url), concurrency=1, rate=0.0
        )

        assert len(reports) == 3
        assert all(r.status is CheckStatus.VALID for r in reports)
        async with open_session(db_url) as session:
            rows = list(await session.scalars(select(Resource)))
            assert {r.check_status for r in rows} == {CheckStatus.VALID}
            assert all(r.last_checked_at is not None for r in rows)

    @pytest.mark.asyncio
    async def test_concurrency_and_multiple_flushes_update_each_resource_once(
        self, db_url, monkeypatch
    ) -> None:
        monkeypatch.setattr(cr, "get_probe", fake_get_probe)
        async with open_session(db_url) as session:
            session.add_all([make_resource(n) for n in range(1, 7)])
            await session.commit()

        reports = cr.run_verify_pipeline(
            settings=Settings(database_url=db_url),
            concurrency=3,
            batch_size=2,
            write_batch=2,
            rate=0.0,
        )

        assert len(reports) == 6
        assert {r.resource_id for r in reports} == set(range(1, 7))
        async with open_session(db_url) as session:
            rows = list(await session.scalars(select(Resource)))
            assert all(r.check_attempts == 1 for r in rows), "重复校验了同一条资源"

    @pytest.mark.asyncio
    async def test_probe_exception_becomes_error_outcome_not_crash(
        self, db_url, monkeypatch
    ) -> None:
        """处理单元线程里探针抛异常不能让流水线崩掉——兜底成 ERROR 结论传给消费者。"""
        monkeypatch.setattr(cr, "get_probe", fake_get_probe)
        async with open_session(db_url) as session:
            session.add(make_resource(1, share_id="boom001"))
            await session.commit()

        reports = cr.run_verify_pipeline(
            settings=Settings(database_url=db_url), concurrency=1, rate=0.0
        )

        assert len(reports) == 1
        assert reports[0].status is CheckStatus.ERROR
        async with open_session(db_url) as session:
            resource = await session.get(Resource, 1)
            assert resource.check_status is CheckStatus.ERROR
            assert resource.next_check_at is not None, "ERROR 要退避重试，不是永久停手"

    @pytest.mark.asyncio
    async def test_progress_callback_reports_enqueued_and_done_counts(
        self, db_url, monkeypatch
    ) -> None:
        """`on_progress(total_enqueued, total_done)` 轮询驱动，不绑死在落库批大小上。"""
        monkeypatch.setattr(cr, "get_probe", fake_get_probe)
        async with open_session(db_url) as session:
            session.add_all([make_resource(n) for n in range(1, 6)])
            await session.commit()

        calls: list[tuple[int, int]] = []
        reports = cr.run_verify_pipeline(
            settings=Settings(database_url=db_url),
            concurrency=1,
            write_batch=100,
            rate=0.0,
            on_progress=lambda total, done: calls.append((total, done)),
        )

        assert len(reports) == 5
        assert calls, "轮询进度回调至少要触发一次"
        total, done = calls[-1]
        assert total == done == 5

    @pytest.mark.asyncio
    async def test_limit_caps_how_many_resources_are_processed(
        self, db_url, monkeypatch
    ) -> None:
        monkeypatch.setattr(cr, "get_probe", fake_get_probe)
        async with open_session(db_url) as session:
            session.add_all([make_resource(n) for n in range(1, 6)])
            await session.commit()

        reports = cr.run_verify_pipeline(
            settings=Settings(database_url=db_url), concurrency=2, rate=0.0, limit=2
        )

        assert len(reports) == 2

    @pytest.mark.asyncio
    async def test_empty_queue_returns_no_reports(self, db_url, monkeypatch) -> None:
        monkeypatch.setattr(cr, "get_probe", fake_get_probe)
        reports = cr.run_verify_pipeline(
            settings=Settings(database_url=db_url), concurrency=2, rate=0.0
        )
        assert reports == []

    @pytest.mark.asyncio
    async def test_recheck_all_false_skips_already_valid_resources(
        self, db_url, monkeypatch
    ) -> None:
        monkeypatch.setattr(cr, "get_probe", fake_get_probe)
        async with open_session(db_url) as session:
            now = utcnow()
            session.add(
                make_resource(
                    1,
                    check_status=CheckStatus.VALID,
                    last_checked_at=now,
                    next_check_at=now + __import__("datetime").timedelta(days=7),
                )
            )
            session.add(make_resource(2))
            await session.commit()

        reports = cr.run_verify_pipeline(
            settings=Settings(database_url=db_url), concurrency=1, rate=0.0
        )

        assert len(reports) == 1
        assert reports[0].resource_id == 2


class TestCountDue:
    @pytest.mark.asyncio
    async def test_counts_only_due_resources(self, session) -> None:
        now = utcnow()
        session.add(make_resource(1))
        session.add(
            make_resource(
                2,
                check_status=CheckStatus.VALID,
                last_checked_at=now,
                next_check_at=now + __import__("datetime").timedelta(days=7),
            )
        )
        session.add(make_resource(3, provider=Provider.BAIDU, share_id="baidu1"))
        await session.commit()

        assert await cr.count_due(session, recheck_all=False, limit=None) == 1

    @pytest.mark.asyncio
    async def test_recheck_all_ignores_next_check_at(self, session) -> None:
        now = utcnow()
        session.add(
            make_resource(
                1,
                check_status=CheckStatus.VALID,
                last_checked_at=now,
                next_check_at=now + __import__("datetime").timedelta(days=7),
            )
        )
        await session.commit()

        assert await cr.count_due(session, recheck_all=True, limit=None) == 1

    @pytest.mark.asyncio
    async def test_limit_caps_the_count(self, session) -> None:
        session.add_all([make_resource(n) for n in range(1, 6)])
        await session.commit()

        assert await cr.count_due(session, recheck_all=False, limit=2) == 2
