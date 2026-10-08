from __future__ import annotations

import httpx
import pytest

from funflix.base.enums import CheckStatus, Provider
from funflix.models import LinkCheck, Resource, utcnow
from funflix.services.verify.alipan import classify as alipan_classify
from funflix.services.verify.base import CheckOutcome, LinkRef
from funflix.services.verify.ctfile import CTFileProbe
from funflix.services.verify.ctfile import classify as ctfile_classify
from funflix.services.verify.pan123 import Pan123Probe
from funflix.services.verify.pan123 import classify as pan123_classify
from funflix.services.verify.quark import QuarkProbe
from funflix.services.verify.quark import classify as quark_classify
from funflix.services.verify.registry import (
    assert_registry_matches_enum,
    get_probe,
    supported_providers,
)
from funflix.services.verify.runner import check_resource


class TestRegistry:
    def test_registry_matches_checkable_enum(self) -> None:
        """注册表与枚举不一致会导致资源永远卡在 unchecked 或永不被调度。"""
        assert_registry_matches_enum()

    def test_supported_providers(self) -> None:
        assert supported_providers() == [
            Provider.ALIPAN,
            Provider.CTFILE,
            Provider.PAN123,
            Provider.QUARK,
            Provider.UC,
        ]

    def test_unsupported_provider_has_no_probe(self) -> None:
        assert get_probe(Provider.BAIDU) is None

    def test_all_probes_are_anonymous(self) -> None:
        """能匿名探测就绝不登录 —— 无凭证依赖、无账号风险。"""
        assert all(not get_probe(p).needs_auth for p in supported_providers())


class TestQuarkClassify:
    def test_valid_share(self) -> None:
        outcome = quark_classify(
            {
                "code": 0,
                "data": {
                    "title": "示例",
                    "expired_type": 1,
                    "author": {"nick_name": "分享者", "avatar_url": "https://img.example/a"},
                },
            },
            200,
        )
        assert outcome.status is CheckStatus.VALID
        assert outcome.title == "示例"
        assert outcome.sharer_name == "分享者"
        assert outcome.sharer_avatar_url == "https://img.example/a"

    def test_missing_share_is_invalid(self) -> None:
        outcome = quark_classify({"code": 41006, "message": "分享不存在"}, 404)
        assert outcome.status is CheckStatus.INVALID

    def test_deleted_file_is_invalid(self) -> None:
        """`41004 文件不存在`：分享还在、分享里的文件被删了。

        对使用者和 `41006` 没区别（点进去拿不到东西），所以同样判 INVALID。
        此前它落到 ERROR —— 而 ERROR 的语义是「判不出来，排退避重试」，于是
        生产库里 57,697 条这样的链接每轮都被重探一遍、永远探不出结论，
        还挤掉了真正待校验链接的名额。
        """
        outcome = quark_classify({"status": 404, "code": 41004, "message": "文件不存在"}, 404)
        assert outcome.status is CheckStatus.INVALID

    def test_banned_sharer_is_invalid(self) -> None:
        outcome = quark_classify({"code": 41031, "message": "分享者用户封禁链接查看受限"}, 403)
        assert outcome.status is CheckStatus.INVALID

    def test_message_hint_without_known_code(self) -> None:
        outcome = quark_classify({"code": 99999, "message": "分享已失效"}, 400)
        assert outcome.status is CheckStatus.INVALID

    def test_password_required(self) -> None:
        outcome = quark_classify({"code": 41005, "message": "请输入提取码"}, 400)
        assert outcome.status is CheckStatus.NEED_PASSWORD

    def test_rate_limited(self) -> None:
        outcome = quark_classify({"code": 41013, "message": "操作过于频繁"}, 429)
        assert outcome.status is CheckStatus.RATE_LIMITED

    def test_unknown_response_returns_none(self) -> None:
        """看不懂的响应返回 None，由骨架归到 ERROR。

        classify 自己**不造** INVALID 兜底 —— 接口改版时那会把整库资源误杀一遍。
        「归 ERROR」这件事由 AnonymousHttpProbe 统一保证，见
        TestUnknownResponseBecomesError。
        """
        assert quark_classify({"code": 12345, "message": "某种新情况"}, 200) is None


class TestAlipanClassify:
    def test_valid_share(self) -> None:
        outcome = alipan_classify(
            {
                "share_name": "示例",
                "expiration": None,
                "creator_id": "123",
                "creator_name": "分享者",
                "avatar": "https://img.example/a",
            },
            200,
        )
        assert outcome.status is CheckStatus.VALID
        assert outcome.title == "示例"
        assert outcome.sharer_id == "123"
        assert outcome.sharer_name == "分享者"

    def test_missing_share_is_invalid(self) -> None:
        outcome = alipan_classify({"code": "NotFound.ShareLink"}, 404)
        assert outcome.status is CheckStatus.INVALID

    def test_password_required(self) -> None:
        outcome = alipan_classify({"has_pwd": True, "share_name": "示例"}, 200)
        assert outcome.status is CheckStatus.NEED_PASSWORD

    def test_unknown_code_returns_none(self) -> None:
        assert alipan_classify({"code": "SomeNewError"}, 400) is None


class TestPan123Classify:
    def test_valid_share(self) -> None:
        outcome = pan123_classify(
            {
                "code": 0,
                "data": {
                    "Len": 1,
                    "Expired": False,
                    "InfoList": [{"FileName": "示例", "Size": 123}],
                },
            },
            200,
        )
        assert outcome.status is CheckStatus.VALID
        assert (outcome.title, outcome.size_bytes) == ("示例", 123)

    def test_password_and_missing_share(self) -> None:
        password = pan123_classify({"code": 5103, "message": "提取码错误"}, 200)
        missing = pan123_classify({"code": 5103, "message": "此分享不存在"}, 200)
        assert password.status is CheckStatus.NEED_PASSWORD
        assert missing.status is CheckStatus.INVALID


class TestCTFileClassify:
    def test_valid_share_with_sharer(self) -> None:
        outcome = ctfile_classify(
            {
                "code": 200,
                "file": {
                    "file_id": 2,
                    "file_name": "示例",
                    "file_size": "844.13 MB",
                    "userid": 1,
                    "username": "分享者",
                },
            },
            200,
        )
        assert outcome.status is CheckStatus.VALID
        assert (outcome.title, outcome.sharer_id, outcome.sharer_name) == ("示例", "1", "分享者")
        assert outcome.size_bytes == int(844.13 * 1024**2)

    def test_password_and_missing_share(self) -> None:
        password = ctfile_classify({"code": 423, "file": {}}, 200)
        missing = ctfile_classify({"code": 404, "file": {"message": "已失效"}}, 200)
        assert password.status is CheckStatus.NEED_PASSWORD
        assert missing.status is CheckStatus.INVALID

    def test_unknown_response_returns_none(self) -> None:
        assert ctfile_classify({"code": 403, "file": {"message": "Forbidden"}}, 200) is None


class TestCheckOutcome:
    @pytest.mark.parametrize(
        ("status", "conclusive"),
        [
            (CheckStatus.VALID, True),
            (CheckStatus.INVALID, True),
            (CheckStatus.NEED_PASSWORD, True),
            (CheckStatus.RATE_LIMITED, False),
            (CheckStatus.ERROR, False),
        ],
    )
    def test_conclusiveness(self, status: CheckStatus, conclusive: bool) -> None:
        """限流和探针异常不是关于链接的结论。"""
        assert CheckOutcome(status=status).is_conclusive is conclusive


def _probe(handler) -> QuarkProbe:
    return QuarkProbe(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
class TestProbeTransport:
    async def test_sends_share_id_and_passcode(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "data": {}})

        await _probe(handler).check(
            LinkRef(Provider.QUARK, "abc123", "https://pan.quark.cn/s/abc123", "8k2m")
        )
        assert seen == {"pwd_id": "abc123", "passcode": "8k2m"}

    async def test_non_json_response_is_error(self) -> None:
        outcome = await _probe(
            lambda r: httpx.Response(502, text="<html>bad gateway</html>")
        ).check(LinkRef(Provider.QUARK, "abc", "u"))
        assert outcome.status is CheckStatus.ERROR

    async def test_network_failure_is_error_not_invalid(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连接失败")

        outcome = await _probe(handler).check(LinkRef(Provider.QUARK, "abc", "u"))
        assert outcome.status is CheckStatus.ERROR
        assert "ConnectError" in outcome.detail

    async def test_pan123_get_params_and_owner_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.params["shareKey"] == "7Tx1jv-yrDiv"
            assert request.url.params["SharePwd"] == "xoxo"
            assert request.content == b""
            return httpx.Response(200, json={"code": 0, "data": {"InfoList": []}})

        probe = Pan123Probe(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        outcome = await probe.check(
            LinkRef(Provider.PAN123, "7Tx1jv-yrDiv", "https://www.123pan.com/s/x", "xoxo")
        )
        assert outcome.status is CheckStatus.VALID
        assert outcome.sharer_id == "1820645299"

    async def test_ctfile_get_params_include_url_password(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/getfile.php"
            assert request.url.params["path"] == "file"
            assert request.url.params["f"] == "123-456"
            assert request.url.params["passcode"] == "abcd"
            return httpx.Response(200, json={"code": 200, "file": {"file_id": 456}})

        probe = CTFileProbe(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        outcome = await probe.check(
            LinkRef(
                Provider.CTFILE,
                "file/123-456",
                "https://www.400gb.com/file/123-456?p=abcd",
            )
        )
        assert outcome.status is CheckStatus.VALID


class StubProbe:
    name = "stub-probe"
    provider = Provider.QUARK
    needs_auth = False

    def __init__(self, outcome: CheckOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    async def check(self, ref: LinkRef) -> CheckOutcome:
        self.calls += 1
        return self._outcome


async def _make_resource(session, provider=Provider.QUARK, status=CheckStatus.UNCHECKED):
    now = utcnow()
    resource = Resource(
        provider=provider,
        share_id="abc123",
        url="https://pan.quark.cn/s/abc123",
        check_status=status,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(resource)
    await session.flush()
    return resource


@pytest.mark.asyncio
class TestCheckResource:
    async def test_valid_updates_status_and_schedules_recheck(self, session) -> None:
        resource = await _make_resource(session)
        report = await check_resource(
            session, resource, StubProbe(CheckOutcome(CheckStatus.VALID, 200, title="示例"))
        )
        await session.commit()

        assert report.status is CheckStatus.VALID
        assert resource.check_status is CheckStatus.VALID
        assert resource.last_checked_at is not None
        assert resource.next_check_at is not None  # 有效链接会定期复查

    async def test_appends_history_row(self, session) -> None:
        from sqlalchemy import select

        resource = await _make_resource(session)
        await check_resource(session, resource, StubProbe(CheckOutcome(CheckStatus.INVALID)))
        await session.commit()

        checks = list(await session.scalars(select(LinkCheck)))
        assert len(checks) == 1
        assert checks[0].status is CheckStatus.INVALID
        assert checks[0].probe == "stub-probe"

    async def test_repeated_invalid_eventually_stops_rechecking(self, session) -> None:
        resource = await _make_resource(session)
        probe = StubProbe(CheckOutcome(CheckStatus.INVALID))
        for _ in range(3):
            await check_resource(session, resource, probe)
        await session.commit()

        # 连续确认失效后不再浪费请求
        assert resource.next_check_at is None

    async def test_error_backs_off_instead_of_marking_invalid(self, session) -> None:
        """探针挂了不能把链接标成失效。"""
        resource = await _make_resource(session)
        report = await check_resource(session, resource, StubProbe(CheckOutcome(CheckStatus.ERROR)))
        await session.commit()

        assert report.status is CheckStatus.ERROR
        assert resource.check_status is not CheckStatus.INVALID
        assert resource.next_check_at is not None  # 退避重试

    async def test_need_password_is_not_auto_rechecked(self, session) -> None:
        resource = await _make_resource(session)
        await check_resource(session, resource, StubProbe(CheckOutcome(CheckStatus.NEED_PASSWORD)))
        await session.commit()
        # 缺提取码不会自己好，等人工补码
        assert resource.next_check_at is None

    async def test_unsupported_provider_is_marked_without_probing(self, session) -> None:
        resource = await _make_resource(session, provider=Provider.BAIDU)
        report = await check_resource(session, resource)
        await session.commit()

        assert report.status is CheckStatus.UNSUPPORTED
        assert resource.next_check_at is None

    async def test_backfills_title_from_netdisk(self, session) -> None:
        resource = await _make_resource(session)
        await check_resource(
            session,
            resource,
            StubProbe(CheckOutcome(CheckStatus.VALID, title="网盘侧标题", size_bytes=123)),
        )
        await session.commit()
        assert resource.title_raw == "网盘侧标题"
        assert resource.size_bytes == 123

    async def test_backfills_sharer_from_netdisk(self, session) -> None:
        resource = await _make_resource(session)
        await check_resource(
            session,
            resource,
            StubProbe(
                CheckOutcome(
                    CheckStatus.VALID,
                    sharer_id="123",
                    sharer_name="分享者",
                    sharer_avatar_url="https://img.example/a",
                )
            ),
        )
        await session.commit()
        assert (resource.sharer_id, resource.sharer_name, resource.sharer_avatar_url) == (
            "123",
            "分享者",
            "https://img.example/a",
        )


@pytest.mark.asyncio
class TestRateLimiter:
    async def test_spaces_out_requests_for_same_provider(self) -> None:
        import time

        from funflix.services.verify.runner import RateLimiter

        limiter = RateLimiter(rate_per_second=20.0)
        started = time.monotonic()
        for _ in range(3):
            await limiter.acquire(Provider.QUARK)
        # 3 次请求至少要间隔 2 个周期
        assert time.monotonic() - started >= 0.09

    async def test_zero_rate_disables_limiting(self) -> None:
        from funflix.services.verify.runner import RateLimiter

        limiter = RateLimiter(rate_per_second=0, overrides={})
        await limiter.acquire(Provider.QUARK)  # 不应阻塞


class TestProviderRateOverrides:
    """按网盘覆盖限速，见 `runner.PROVIDER_RATE_LIMITS`。

    两个限流器实现（async / 线程版）共享同一张折算逻辑，所以除了那条验证
    "覆盖值真的会卡住请求"的计时断言，其余都对两个实现各跑一遍。
    """

    def _classes(self) -> tuple[type, type]:
        from funflix.services.verify.runner import BlockingRateLimiter, RateLimiter

        return RateLimiter, BlockingRateLimiter

    def test_alipan_is_slower_than_the_global_default(self) -> None:
        # 阿里云盘在默认 5 次/秒下实测 300 条有 238 条返回 TooManyRequests，
        # 必须单独调慢；真要改这个值，先把 runner.py 里那张实测表重测一遍。
        from funflix.services.verify.runner import PROVIDER_RATE_LIMITS

        assert PROVIDER_RATE_LIMITS[Provider.ALIPAN] < 5.0

    def test_override_applies_to_listed_provider_only(self) -> None:
        for cls in self._classes():
            limiter = cls(rate_per_second=10.0, overrides={Provider.ALIPAN: 2.0})
            assert limiter._interval_for(Provider.ALIPAN) == pytest.approx(0.5), cls
            assert limiter._interval_for(Provider.QUARK) == pytest.approx(0.1), cls

    def test_empty_overrides_leaves_one_global_rate(self) -> None:
        for cls in self._classes():
            limiter = cls(rate_per_second=10.0, overrides={})
            assert limiter._interval_for(Provider.ALIPAN) == pytest.approx(0.1), cls

    def test_default_overrides_are_the_production_table(self) -> None:
        from funflix.services.verify.runner import PROVIDER_RATE_LIMITS

        for cls in self._classes():
            limiter = cls(rate_per_second=10.0)
            expected = 1.0 / PROVIDER_RATE_LIMITS[Provider.ALIPAN]
            assert limiter._interval_for(Provider.ALIPAN) == pytest.approx(expected), cls

    def test_override_actually_gates_requests(self) -> None:
        import time

        from funflix.services.verify.runner import BlockingRateLimiter

        limiter = BlockingRateLimiter(rate_per_second=1000.0, overrides={Provider.ALIPAN: 20.0})
        started = time.monotonic()
        for _ in range(3):
            limiter.acquire(Provider.ALIPAN)
        # 3 次请求至少要间隔 2 个 50ms 周期
        assert time.monotonic() - started >= 0.09

        # 没被覆盖的网盘仍按全局速率走，不受阿里的慢速牵连
        started = time.monotonic()
        for _ in range(3):
            limiter.acquire(Provider.QUARK)
        assert time.monotonic() - started < 0.09


class TestAdaptiveInterval:
    """限流反馈：被限流就放慢，拿到明确结论就慢慢收回。

    为什么需要自适应而不是把 `PROVIDER_RATE_LIMITS` 的常数再往下调一档 ——
    那个常数定不准。阿里云盘 1.0 次/秒在 25 条样本上测出 8% 被限流，线上长跑
    （run 37706256433，3,657 次调用）是 33%，差的是持续量顶穿了小时级配额。
    配额看不见、也会变，所以实际速率只能靠反馈收敛。
    """

    def _table(self, **kw):
        from funflix.services.verify.runner import _IntervalTable

        kw.setdefault("rate_per_second", 10.0)
        return _IntervalTable(**kw)

    def test_rate_limited_lengthens_the_interval(self) -> None:
        from funflix.services.verify.runner import _PENALTY_FACTOR

        table = self._table()
        base = table.interval_for(Provider.QUARK)
        table.on_rate_limited(Provider.QUARK)
        assert table.interval_for(Provider.QUARK) == pytest.approx(base * _PENALTY_FACTOR)

    def test_penalty_only_touches_the_provider_that_complained(self) -> None:
        table = self._table()
        before = table.interval_for(Provider.ALIPAN)
        table.on_rate_limited(Provider.QUARK)
        assert table.interval_for(Provider.ALIPAN) == pytest.approx(before)

    def test_conclusive_results_walk_the_interval_back(self) -> None:
        table = self._table()
        base = table.interval_for(Provider.QUARK)
        for _ in range(5):
            table.on_rate_limited(Provider.QUARK)
        slowed = table.interval_for(Provider.QUARK)
        assert slowed > base
        for _ in range(50):
            table.on_conclusive(Provider.QUARK)
        assert table.interval_for(Provider.QUARK) < slowed

    def test_never_goes_faster_than_the_measured_floor(self) -> None:
        """`PROVIDER_RATE_LIMITS` 是下限，自适应只许往慢的方向走。

        那张表是实测出来的风控线，不是调优起点；一路顺利就加速会直接撞回去。
        """
        table = self._table()
        base = table.interval_for(Provider.QUARK)
        for _ in range(500):
            table.on_conclusive(Provider.QUARK)
        assert table.interval_for(Provider.QUARK) == pytest.approx(base)

    def test_slowdown_is_capped(self) -> None:
        """封顶是防一段网络抖动把某个网盘永久摁死，见 `_MAX_INTERVAL_FACTOR`。"""
        from funflix.services.verify.runner import _MAX_INTERVAL_FACTOR

        table = self._table()
        base = table.interval_for(Provider.QUARK)
        for _ in range(200):
            table.on_rate_limited(Provider.QUARK)
        assert table.interval_for(Provider.QUARK) == pytest.approx(base * _MAX_INTERVAL_FACTOR)

    def test_unthrottled_providers_stay_unthrottled(self) -> None:
        """速率设成 0 是调用方明确要求别节流，反馈不能偷偷把它变成限流的。

        `verify --resource-id` 单条校验和大量测试都依赖这个：一次调用就被罚
        一下、下一次就开始睡，整个套件会慢得莫名其妙。
        """
        table = self._table(rate_per_second=0.0, overrides={})
        for _ in range(10):
            table.on_rate_limited(Provider.QUARK)
        assert table.interval_for(Provider.QUARK) == 0.0
        assert table.factors() == {}

    def test_factors_report_only_the_slowed_providers(self) -> None:
        table = self._table()
        table.on_rate_limited(Provider.ALIPAN)
        table.on_conclusive(Provider.QUARK)
        assert list(table.factors()) == [Provider.ALIPAN]

    def test_limiters_expose_the_same_table(self) -> None:
        """两个限流器实现共用同一个类，退避逻辑不许在两处各长一份。

        理由同 `base/backoff.py` 的模块 docstring：参数一旦悄悄分叉，
        "为什么这一层重试得特别猛"会变成很难查的问题。
        """
        from funflix.services.verify.runner import (
            BlockingRateLimiter,
            RateLimiter,
            _IntervalTable,
        )

        for cls in (RateLimiter, BlockingRateLimiter):
            limiter = cls(rate_per_second=10.0)
            assert isinstance(limiter.intervals, _IntervalTable), cls
            limiter.intervals.on_rate_limited(Provider.QUARK)
            assert limiter._interval_for(Provider.QUARK) == pytest.approx(0.15), cls

    def test_feedback_actually_gates_requests(self) -> None:
        """放慢之后真的要睡得更久 —— 不然系数只是个好看的数字。"""
        import time

        from funflix.services.verify.runner import BlockingRateLimiter

        limiter = BlockingRateLimiter(rate_per_second=1000.0, overrides={Provider.ALIPAN: 50.0})
        # 基准 20ms 一次，连罚 4 次后是 20ms × 1.5^4 ≈ 101ms
        for _ in range(4):
            limiter.intervals.on_rate_limited(Provider.ALIPAN)

        limiter.acquire(Provider.ALIPAN)
        started = time.monotonic()
        limiter.acquire(Provider.ALIPAN)
        assert time.monotonic() - started >= 0.09
