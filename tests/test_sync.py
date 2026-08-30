"""本地库/远端库同步（`funflix sync pull` / `push`）。

用两个独立的内存 SQLite 引擎模拟 local/remote —— 同步逻辑本身跟"两边是不是
同一个进程、同一个物理机"无关，只关心两个 `AsyncSession` 之间的数据流动。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from funflix.base.enums import SourceType
from funflix.models import Base, RawDocument, Source, utcnow
from funflix.models.base import uuid7
from funflix.services.sync import TableSyncResult, pull, push

BASE = utcnow()


async def _make_session(*, create_schema: bool = True) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    if create_schema:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def local_session() -> AsyncIterator[AsyncSession]:
    async for session in _make_session():
        yield session


@pytest_asyncio.fixture
async def blank_local_session() -> AsyncIterator[AsyncSession]:
    """没有预先建表的本地库——模拟 CI runner 上全新的 SQLite 文件。"""
    async for session in _make_session(create_schema=False):
        yield session


@pytest_asyncio.fixture
async def remote_session() -> AsyncIterator[AsyncSession]:
    async for session in _make_session():
        yield session


def _source(n: int = 1, **overrides) -> Source:
    kwargs = dict(
        source_type=SourceType.TELEGRAM,
        url=f"https://t.me/s/ch{n}",
        identifier=f"ch{n}",
        enabled=True,
        extra={},
    )
    kwargs.update(overrides)
    return Source(**kwargs)


def _doc(n: int = 1, content_hash: str | None = None, **overrides) -> RawDocument:
    kwargs = dict(
        content=f"doc{n}",
        content_hash=content_hash or f"{n:064d}",
        source_type=SourceType.MANUAL,
        collected_at=utcnow(),
        extra={},
    )
    kwargs.update(overrides)
    return RawDocument(**kwargs)


def _result(report, table: str) -> TableSyncResult:
    return next(t for t in report.tables if t.table == table)


@pytest.mark.asyncio
class TestPull:
    async def test_creates_local_schema_when_missing(self, blank_local_session, remote_session):
        """回归：CI runner 每次都是全新机器，本地 SQLite 文件里连表都没有——
        `pull` 不能假设本地库已经建过表，得自己先建。"""
        remote_session.add(_source())
        await remote_session.commit()

        report = await pull(blank_local_session, remote_session)

        assert _result(report, "source").applied == 1
        rows = (await blank_local_session.execute(select(Source))).scalars().all()
        assert [r.identifier for r in rows] == ["ch1"]

    async def test_bootstraps_empty_local_from_remote(self, local_session, remote_session):
        remote_session.add(_source())
        await remote_session.commit()

        report = await pull(local_session, remote_session)

        assert _result(report, "source").applied == 1
        rows = (await local_session.execute(select(Source))).scalars().all()
        assert [r.identifier for r in rows] == ["ch1"]

    async def test_old_rows_drop_out_of_the_overlap_window_over_time(
        self, local_session, remote_session
    ):
        """回归：水位线只回退 5 分钟安全窗口，不是无限回看——早已同步过的行
        不该在每一轮里被重新扫一遍。"""
        t0, t1, t2 = BASE - timedelta(hours=2), BASE, BASE + timedelta(hours=1)

        remote_session.add(_source(1, created_at=t0, updated_at=t0))
        await remote_session.commit()
        await pull(local_session, remote_session)  # local 水位 = t0

        remote_session.add(_source(2, created_at=t1, updated_at=t1))
        await remote_session.commit()
        report = await pull(local_session, remote_session)  # since = t0 - 5min，t0/t1 都在内
        assert _result(report, "source").fetched == 2

        remote_session.add(_source(3, created_at=t2, updated_at=t2))
        await remote_session.commit()
        report = await pull(local_session, remote_session)  # since = t1 - 5min，t0 已经过期
        assert _result(report, "source").fetched == 2


@pytest.mark.asyncio
class TestPush:
    async def test_is_idempotent(self, local_session, remote_session):
        local_session.add(_source())
        await local_session.commit()

        first = await push(local_session, remote_session)
        second = await push(local_session, remote_session)

        assert _result(first, "source").applied == 1
        assert _result(second, "source").applied == 1
        rows = (await remote_session.execute(select(Source))).scalars().all()
        assert len(rows) == 1

    async def test_does_not_clobber_a_newer_remote_row(self, local_session, remote_session):
        """last-write-wins：本地这一份如果比远端旧，push 不能覆盖远端的新值。"""
        shared_id = uuid7()
        stale_local = _source(
            id=shared_id, identifier="ch1-stale", created_at=BASE, updated_at=BASE
        )
        fresh_remote = _source(
            id=shared_id,
            identifier="ch1-fresh",
            created_at=BASE,
            updated_at=BASE + timedelta(hours=1),
        )
        local_session.add(stale_local)
        remote_session.add(fresh_remote)
        await local_session.commit()
        await remote_session.commit()

        await push(local_session, remote_session)

        remote_row = await remote_session.get(Source, shared_id)
        assert remote_row.identifier == "ch1-fresh"

    async def test_skips_secondary_unique_key_conflict_without_aborting_batch(
        self, local_session, remote_session
    ):
        """`content_hash` 是主键之外的业务唯一键：本地和远端各自独立生成了
        不同的 uuid7 主键、但内容相同时，`ON CONFLICT(id)` 保护不到这种冲突，
        必须降级为逐行处理，跳过冲突的那一行，不拖累同批次其余正常的行。"""
        shared_hash = "a" * 64
        remote_session.add(_doc(1, content_hash=shared_hash))
        await remote_session.commit()

        conflicting = _doc(2, content_hash=shared_hash)
        clean = _doc(3, content_hash="b" * 64)
        local_session.add_all([conflicting, clean])
        await local_session.commit()

        report = await push(local_session, remote_session)

        result = _result(report, "raw_document")
        assert result.fetched == 2
        assert result.applied == 1
        assert result.skipped_conflicts == 1
        remote_rows = (await remote_session.execute(select(RawDocument))).scalars().all()
        assert {r.content_hash for r in remote_rows} == {shared_hash, "b" * 64}
