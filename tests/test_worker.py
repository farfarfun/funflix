"""worker 的领取语义与任务执行。

这一层存在的全部理由就是并发正确性，所以测试的重点不是"能跑通"，
而是"两个 worker 抢同一条任务时只有一个能拿到"以及
"崩溃的任务会回到队列，但不会无限重捞"。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from funflix.base.backoff import BASE_BACKOFF, MAX_BACKOFF, backoff
from funflix.base.config import Settings
from funflix.base.enums import CheckStatus, ParseStatus, Provider, Quality, SourceType
from funflix.models import Base, LinkCheck, RawDocument, Resource, Source, utcnow
from funflix.services.extract.runner import MAX_PARSE_ATTEMPTS
from funflix.services.verify.base import CheckOutcome
from funflix.worker.claim import claim_documents, claim_resources, claim_sources
from funflix.worker.scheduler import Worker, stale_summary
from funflix.worker.tasks import run_parse_batch, run_verify_batch

# --- 夹具 --------------------------------------------------------------------


def make_doc(n: int, **kwargs) -> RawDocument:
    """一条待解析文本。内容里带一个假链接，让规则抽取器有东西可抽。"""
    defaults = dict(
        content=f"名称：测试剧集{n}\n链接：https://pan.quark.cn/s/fake{n:06d}",
        content_hash=f"hash{n:060d}",
        source_type=SourceType.MANUAL,
        collected_at=utcnow(),
        parse_status=ParseStatus.PENDING,
        extra={},
    )
    return RawDocument(**{**defaults, **kwargs})


def make_resource(n: int, **kwargs) -> Resource:
    now = utcnow()
    defaults = dict(
        provider=Provider.QUARK,
        share_id=f"share{n:06d}",
        url=f"https://pan.quark.cn/s/share{n:06d}",
        quality=Quality.UNKNOWN,
        check_status=CheckStatus.UNCHECKED,
        next_check_at=now,
        first_seen_at=now,
        last_seen_at=now,
    )
    return Resource(**{**defaults, **kwargs})


def make_source(n: int, **kwargs) -> Source:
    defaults = dict(
        source_type=SourceType.TELEGRAM,
        url=f"https://t.me/s/channel{n}",
        identifier=f"channel{n}",
        enabled=True,
        extra={},
    )
    return Source(**{**defaults, **kwargs})


class StubProbe:
    """固定结论的假探针。真探针会发网络请求，测试不能依赖网盘可达。"""

    name = "stub"
    provider = Provider.QUARK
    needs_auth = False

    def __init__(self, status: CheckStatus = CheckStatus.VALID) -> None:
        self.status = status
        self.calls = 0

    async def check(self, ref) -> CheckOutcome:
        self.calls += 1
        return CheckOutcome(status=self.status, http_code=200, detail="stub", latency_ms=1)


@pytest_asyncio.fixture
async def two_sessions(tmp_path):
    """同一个库上的两个独立会话，各自持有自己的连接。

    内存库用的是 StaticPool，所有会话复用同一条连接，模拟不出两个进程
    真正并发抢同一行的情形 —— 所以这里落到文件库。
    """
    url = f"sqlite+aiosqlite:///{tmp_path}/worker.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as a, maker() as b:
        yield a, b
    await engine.dispose()


# --- 退避 --------------------------------------------------------------------


class TestBackoff:
    def test_grows_exponentially(self) -> None:
        assert backoff(1) == timedelta(minutes=2)
        assert backoff(2) == timedelta(minutes=4)
        assert backoff(3) == timedelta(minutes=8)

    def test_caps_at_max(self) -> None:
        assert backoff(20) == MAX_BACKOFF

    def test_zero_attempts_still_waits(self) -> None:
        """立刻重试一个刚失败的任务只会同样再失败一次。"""
        assert backoff(0) == BASE_BACKOFF
        assert backoff(-3) == BASE_BACKOFF

    def test_huge_attempts_does_not_overflow(self) -> None:
        """source.consecutive_failures 没有上限，2**n 会把 timedelta 撑爆。

        一个持续失败的源攒到 ~50 次时，旧写法直接抛 OverflowError，
        把整个采集循环打断。
        """
        assert backoff(10_000) == MAX_BACKOFF


# --- 领取：文档 --------------------------------------------------------------


class TestClaimDocuments:
    @pytest.mark.asyncio
    async def test_claims_pending_and_marks_running(self, session) -> None:
        session.add(make_doc(1))
        await session.commit()

        claimed = await claim_documents(session, limit=10)

        assert len(claimed) == 1
        doc = claimed.rows[0]
        assert doc.parse_status is ParseStatus.RUNNING
        assert doc.lease_until is not None

    @pytest.mark.asyncio
    async def test_respects_limit(self, session) -> None:
        for i in range(5):
            session.add(make_doc(i))
        await session.commit()

        assert len(await claim_documents(session, limit=2)) == 2

    @pytest.mark.asyncio
    async def test_does_not_claim_live_lease(self, session) -> None:
        """租约没过期就是别的 worker 正在处理，不能抢。"""
        session.add(
            make_doc(1, parse_status=ParseStatus.RUNNING, lease_until=utcnow() + timedelta(hours=1))
        )
        await session.commit()

        assert len(await claim_documents(session, limit=10)) == 0

    @pytest.mark.asyncio
    async def test_does_not_claim_before_next_parse_at(self, session) -> None:
        """失败退避期内不该被重新领取。"""
        session.add(make_doc(1, next_parse_at=utcnow() + timedelta(hours=1)))
        await session.commit()

        assert len(await claim_documents(session, limit=10)) == 0

    @pytest.mark.asyncio
    async def test_reclaims_expired_lease_and_counts_attempt(self, session) -> None:
        """崩溃遗留的任务要回到队列，且必须计数 —— 否则毒任务会无限重捞。"""
        session.add(
            make_doc(
                1,
                parse_status=ParseStatus.RUNNING,
                lease_until=utcnow() - timedelta(minutes=1),
                parse_attempts=1,
            )
        )
        await session.commit()

        claimed = await claim_documents(session, limit=10)

        assert claimed.reclaimed == 1
        assert claimed.rows[0].parse_attempts == 2

    @pytest.mark.asyncio
    async def test_normal_claim_does_not_count_attempt(self, session) -> None:
        """正常失败由 parse_document 自己累加，领取时再加一次会让重试次数减半。"""
        session.add(make_doc(1, parse_attempts=2))
        await session.commit()

        claimed = await claim_documents(session, limit=10)

        assert claimed.reclaimed == 0
        assert claimed.rows[0].parse_attempts == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self, session) -> None:
        """反复让 worker 崩溃的文档必须被放弃，否则它会一直把队列堵死。"""
        session.add(
            make_doc(
                1,
                parse_status=ParseStatus.RUNNING,
                lease_until=utcnow() - timedelta(minutes=1),
                parse_attempts=MAX_PARSE_ATTEMPTS - 1,
            )
        )
        await session.commit()

        claimed = await claim_documents(session, limit=10)

        assert len(claimed) == 0
        assert claimed.abandoned == 1
        doc = await session.get(RawDocument, 1)
        await session.refresh(doc)
        assert doc.parse_status is ParseStatus.FAILED
        assert doc.lease_until is None

    @pytest.mark.asyncio
    async def test_second_claim_gets_nothing(self, session) -> None:
        """同一个会话连续领两次，第二次不该拿到同一条。"""
        session.add(make_doc(1))
        await session.commit()

        first = await claim_documents(session, limit=10)
        second = await claim_documents(session, limit=10)

        assert len(first) == 1
        assert len(second) == 0

    @pytest.mark.asyncio
    async def test_two_workers_never_get_the_same_document(self, two_sessions) -> None:
        """核心保证：并发领取不重叠。

        这正是 worker 之前缺失的东西 —— CLI 用的是裸 SELECT，两个进程
        会各自选中同一批文档，把同一条文本送进 LLM 两次。
        """
        a, b = two_sessions
        for i in range(20):
            a.add(make_doc(i))
        await a.commit()

        got_a, got_b = await asyncio.gather(
            claim_documents(a, limit=20),
            claim_documents(b, limit=20),
        )

        ids_a = {d.id for d in got_a.rows}
        ids_b = {d.id for d in got_b.rows}
        assert not (ids_a & ids_b), "同一条文档被两个 worker 同时领走"
        assert len(ids_a) + len(ids_b) == 20, "有文档在竞争中丢失"


# --- 领取：资源与源 ----------------------------------------------------------


class TestClaimResources:
    @pytest.mark.asyncio
    async def test_claims_and_marks_checking(self, session) -> None:
        session.add(make_resource(1))
        await session.commit()

        claimed = await claim_resources(session, limit=10)

        assert len(claimed) == 1
        assert claimed.rows[0].check_status is CheckStatus.CHECKING

    @pytest.mark.asyncio
    async def test_records_prior_status(self, session) -> None:
        """领取会把状态改成 checking 占位，领取前的真实结论必须留下来。"""
        session.add(make_resource(1, check_status=CheckStatus.INVALID, next_check_at=utcnow()))
        await session.commit()

        claimed = await claim_resources(session, limit=10)

        assert claimed.priors[claimed.rows[0].id] is CheckStatus.INVALID

    @pytest.mark.asyncio
    async def test_skips_uncheckable_provider(self, session) -> None:
        """没有探针的网盘领了也没用，只会白占一轮批次。"""
        session.add(make_resource(1, provider=Provider.BAIDU))
        await session.commit()

        assert len(await claim_resources(session, limit=10)) == 0

    @pytest.mark.asyncio
    async def test_skips_resource_not_due_for_recheck(self, session) -> None:
        session.add(
            make_resource(
                1, check_status=CheckStatus.VALID, next_check_at=utcnow() + timedelta(days=3)
            )
        )
        await session.commit()

        assert len(await claim_resources(session, limit=10)) == 0


class TestClaimSources:
    @pytest.mark.asyncio
    async def test_claims_due_source(self, session) -> None:
        session.add(make_source(1))
        await session.commit()

        claimed = await claim_sources(session, limit=10)

        assert len(claimed) == 1
        assert claimed.rows[0].lease_until is not None

    @pytest.mark.asyncio
    async def test_skips_disabled(self, session) -> None:
        session.add(make_source(1, enabled=False))
        await session.commit()

        assert len(await claim_sources(session, limit=10)) == 0

    @pytest.mark.asyncio
    async def test_reclaim_backs_off(self, session) -> None:
        """采集崩溃复用健康度机制退避，而不是每轮都去踩同一个坑。"""
        session.add(make_source(1, lease_until=utcnow() - timedelta(minutes=1)))
        await session.commit()

        claimed = await claim_sources(session, limit=10)

        assert claimed.reclaimed == 1
        assert claimed.rows[0].consecutive_failures == 1
        assert claimed.rows[0].next_fetch_at > utcnow()

    @pytest.mark.asyncio
    async def test_two_workers_never_get_the_same_source(self, two_sessions) -> None:
        """同一个源被两个 worker 同采，会各自推进一次水位，中间的消息就漏了。"""
        a, b = two_sessions
        for i in range(10):
            a.add(make_source(i))
        await a.commit()

        got_a, got_b = await asyncio.gather(
            claim_sources(a, limit=10),
            claim_sources(b, limit=10),
        )

        assert not ({s.id for s in got_a.rows} & {s.id for s in got_b.rows})


# --- 任务执行 ----------------------------------------------------------------


class TestParseBatch:
    @pytest.mark.asyncio
    async def test_parses_and_releases_lease(self, session) -> None:
        session.add(make_doc(1))
        await session.commit()

        report = await run_parse_batch(session, limit=10, extractor="rule")

        assert report.claimed == 1
        assert report.succeeded == 1
        doc = await session.get(RawDocument, 1)
        await session.refresh(doc)
        assert doc.lease_until is None, "租约没归还，这条会空转一整个租约周期"
        assert doc.parse_status is not ParseStatus.RUNNING

    @pytest.mark.asyncio
    async def test_idle_when_queue_empty(self, session) -> None:
        report = await run_parse_batch(session, limit=10, extractor="rule")
        assert report.idle


class TestVerifyBatch:
    @pytest.mark.asyncio
    async def test_verifies_and_releases_lease(self, session, monkeypatch) -> None:
        probe = StubProbe(CheckStatus.VALID)
        monkeypatch.setattr("funflix.worker.tasks.get_probe", lambda _p: probe)
        session.add(make_resource(1))
        await session.commit()

        report = await run_verify_batch(session, limit=10)

        assert report.succeeded == 1
        resource = await session.get(Resource, 1)
        await session.refresh(resource)
        assert resource.check_status is CheckStatus.VALID
        assert resource.lease_until is None
        assert await session.get(LinkCheck, 1) is not None

    @pytest.mark.asyncio
    async def test_consecutive_invalid_stops_rechecking(self, session, monkeypatch) -> None:
        """§6.4：确认两次失效后就不再复查。

        领取时状态被置成 checking，如果拿它当"上一次的结论"来比，连续计数
        每轮都会被重置成 1，这条规则就永远不触发 —— 一条早就没了的链接
        会被永远复查下去。
        """
        monkeypatch.setattr(
            "funflix.worker.tasks.get_probe", lambda _p: StubProbe(CheckStatus.INVALID)
        )
        session.add(make_resource(1))
        await session.commit()

        await run_verify_batch(session, limit=10)
        resource = await session.get(Resource, 1)
        await session.refresh(resource)
        assert resource.check_status is CheckStatus.INVALID
        assert resource.next_check_at is not None, "第一次失效应该再确认一次"

        # 把复查时间拨到现在，模拟 30 天后的那一次复查
        resource.next_check_at = utcnow() - timedelta(seconds=1)
        await session.commit()

        await run_verify_batch(session, limit=10)
        await session.refresh(resource)
        assert resource.next_check_at is None, "连续两次失效后不该再排复查"

    @pytest.mark.asyncio
    async def test_probe_error_counts_as_failure(self, session, monkeypatch) -> None:
        """探针异常不是"链接失效"，是"没探出来"。"""
        monkeypatch.setattr(
            "funflix.worker.tasks.get_probe", lambda _p: StubProbe(CheckStatus.ERROR)
        )
        session.add(make_resource(1))
        await session.commit()

        report = await run_verify_batch(session, limit=10)

        assert report.failed == 1
        assert report.succeeded == 0


# --- 调度 --------------------------------------------------------------------


class TestScheduler:
    def _worker(self, session, **overrides) -> Worker:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def factory():
            yield session

        settings = Settings(worker_extractor="rule", **overrides)
        return Worker(settings, session_factory=factory)

    @pytest.mark.asyncio
    async def test_run_once_is_idle_on_empty_db(self, session) -> None:
        assert (await self._worker(session).run_once()).idle

    @pytest.mark.asyncio
    async def test_run_once_drains_parse_queue(self, session) -> None:
        session.add(make_doc(1))
        await session.commit()

        report = await self._worker(session).run_once()

        assert report.parse.claimed == 1
        assert not report.idle

    @pytest.mark.asyncio
    async def test_stale_summary_counts_crashed_tasks(self, session) -> None:
        session.add(
            make_doc(
                1, parse_status=ParseStatus.RUNNING, lease_until=utcnow() - timedelta(minutes=1)
            )
        )
        session.add(make_doc(2))  # 正常待处理，不该被算进去
        await session.commit()

        stale = await stale_summary(session)

        assert stale.documents == 1
        assert stale.total == 1

    @pytest.mark.asyncio
    async def test_run_forever_stops_on_signal(self, session) -> None:
        """停止信号必须能打断轮询等待，否则关进程要等满一个间隔。"""
        worker = self._worker(session, worker_poll_seconds=3600)
        stop = asyncio.Event()

        task = asyncio.create_task(worker.run_forever(stop))
        await asyncio.sleep(0.05)
        stop.set()

        await asyncio.wait_for(task, timeout=2)
