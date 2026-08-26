from __future__ import annotations

import httpx
import pytest

from funflix.base.enums import CheckStatus, Provider
from funflix.models import LinkCheck, Resource, utcnow
from funflix.services.verify.alipan import classify as alipan_classify
from funflix.services.verify.base import CheckOutcome, LinkRef
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
        assert supported_providers() == [Provider.ALIPAN, Provider.QUARK]

    def test_unsupported_provider_has_no_probe(self) -> None:
        assert get_probe(Provider.BAIDU) is None

    def test_all_probes_are_anonymous(self) -> None:
        """能匿名探测就绝不登录 —— 无凭证依赖、无账号风险。"""
        assert all(not get_probe(p).needs_auth for p in supported_providers())


class TestQuarkClassify:
    def test_valid_share(self) -> None:
        outcome = quark_classify({"code": 0, "data": {"title": "示例", "expired_type": 1}}, 200)
        assert outcome.status is CheckStatus.VALID
        assert outcome.title == "示例"

    def test_missing_share_is_invalid(self) -> None:
        outcome = quark_classify({"code": 41006, "message": "分享不存在"}, 404)
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
        outcome = alipan_classify({"share_name": "示例", "expiration": None}, 200)
        assert outcome.status is CheckStatus.VALID
        assert outcome.title == "示例"

    def test_missing_share_is_invalid(self) -> None:
        outcome = alipan_classify({"code": "NotFound.ShareLink"}, 404)
        assert outcome.status is CheckStatus.INVALID

    def test_password_required(self) -> None:
        outcome = alipan_classify({"has_pwd": True, "share_name": "示例"}, 200)
        assert outcome.status is CheckStatus.NEED_PASSWORD

    def test_unknown_code_returns_none(self) -> None:
        assert alipan_classify({"code": "SomeNewError"}, 400) is None


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
            session, resource, StubProbe(CheckOutcome(CheckStatus.VALID, title="网盘侧标题"))
        )
        await session.commit()
        assert resource.title_raw == "网盘侧标题"


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

        limiter = RateLimiter(rate_per_second=0)
        await limiter.acquire(Provider.QUARK)  # 不应阻塞
