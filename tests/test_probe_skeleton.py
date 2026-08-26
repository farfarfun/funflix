"""探针骨架的安全不变量。

这套系统最危险的错误是**把探针自己的故障当成"链接失效"**：接口改版、被风控、
返回一段 HTML 错误页 —— 任何一种被判成 INVALID，都会在一轮复查里把整库资源
误杀，而且看起来完全正常（状态是"已确认失效"，不是报错）。

所以下面每一条都在问同一个问题：**这条路径会不会产出 INVALID？**
只有网盘明确说"这个分享没了"才允许 INVALID，其余一律 ERROR。
"""

from __future__ import annotations

import httpx
import pytest

from funflix.base.enums import CheckStatus, Provider
from funflix.services.verify.base import AnonymousHttpProbe, CheckOutcome, LinkRef

REF = LinkRef(provider=Provider.QUARK, share_id="abc123", url="https://example.invalid/s/abc123")


class _Probe(AnonymousHttpProbe):
    """只认一个业务码的最小探针，其余一律返回 None。"""

    name = "test-probe"
    provider = Provider.QUARK
    endpoint = "https://example.invalid/api"
    referer = "https://example.invalid/"

    def build_payload(self, ref: LinkRef) -> dict:
        return {"id": ref.share_id}

    def classify(self, payload: dict, http_code: int) -> CheckOutcome | None:
        if payload.get("code") == "GONE":
            return CheckOutcome(status=CheckStatus.INVALID, http_code=http_code, detail="没了")
        if payload.get("code") == "OK":
            return CheckOutcome(status=CheckStatus.VALID, http_code=http_code)
        return None


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
class TestUnknownResponseBecomesError:
    async def test_unrecognised_payload_is_error_not_invalid(self) -> None:
        """classify 返回 None —— 骨架必须归 ERROR。

        这是「忘了写兜底分支」时的默认行为。安全的结果必须是不做任何事就能拿到的，
        否则迟早有人漏掉。
        """
        client = _client(lambda r: httpx.Response(200, json={"code": "BRAND_NEW"}))
        outcome = await _Probe(client).check(REF)
        assert outcome.status is CheckStatus.ERROR
        assert not outcome.is_conclusive

    async def test_html_error_page_is_error(self) -> None:
        """被风控时常返回一段 HTML，不是 JSON。"""
        client = _client(lambda r: httpx.Response(200, text="<html>请稍后再试</html>"))
        outcome = await _Probe(client).check(REF)
        assert outcome.status is CheckStatus.ERROR
        assert "不是 JSON" in (outcome.detail or "")

    async def test_json_but_not_an_object_is_error(self) -> None:
        """合法 JSON 但不是对象（比如一个裸数组），classify 没法处理。"""
        client = _client(lambda r: httpx.Response(200, json=["surprise"]))
        outcome = await _Probe(client).check(REF)
        assert outcome.status is CheckStatus.ERROR

    async def test_network_failure_is_error(self) -> None:
        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连不上", request=request)

        outcome = await _Probe(_client(_boom)).check(REF)
        assert outcome.status is CheckStatus.ERROR

    async def test_timeout_is_error(self) -> None:
        def _slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("超时", request=request)

        outcome = await _Probe(_client(_slow)).check(REF)
        assert outcome.status is CheckStatus.ERROR

    async def test_classify_raising_is_error_not_invalid(self) -> None:
        """解析逻辑自己抛异常时，也不能算链接失效。"""

        class _Broken(_Probe):
            def classify(self, payload: dict, http_code: int) -> CheckOutcome | None:
                raise KeyError("解析写错了")

        outcome = await _Broken(_client(lambda r: httpx.Response(200, json={}))).check(REF)
        assert outcome.status is CheckStatus.ERROR

    async def test_5xx_is_error(self) -> None:
        """网盘自己 500 了，跟这条链接有没有失效无关。"""
        client = _client(lambda r: httpx.Response(503, json={"code": "ServiceUnavailable"}))
        outcome = await _Probe(client).check(REF)
        assert outcome.status is CheckStatus.ERROR


@pytest.mark.asyncio
class TestConclusiveResultsStillWork:
    async def test_explicit_gone_is_invalid(self) -> None:
        """只有网盘明确说"没了"才允许 INVALID。"""
        client = _client(lambda r: httpx.Response(404, json={"code": "GONE"}))
        outcome = await _Probe(client).check(REF)
        assert outcome.status is CheckStatus.INVALID
        assert outcome.is_conclusive

    async def test_valid_is_valid(self) -> None:
        client = _client(lambda r: httpx.Response(200, json={"code": "OK"}))
        outcome = await _Probe(client).check(REF)
        assert outcome.status is CheckStatus.VALID


@pytest.mark.asyncio
class TestSkeletonMechanics:
    async def test_latency_is_recorded_on_every_path(self) -> None:
        """耗时统计不能只在成功路径上有。

        原先「响应不是 JSON」那条分支是提前 return 的，会跳过耗时赋值 ——
        失败路径的延迟恰恰是判断"探针是不是被限速了"的关键信号。
        """
        for handler in (
            lambda r: httpx.Response(200, json={"code": "OK"}),
            lambda r: httpx.Response(200, text="不是 JSON"),
            lambda r: httpx.Response(200, json={"code": "BRAND_NEW"}),
        ):
            outcome = await _Probe(_client(handler)).check(REF)
            assert outcome.latency_ms is not None

    async def test_request_carries_referer_and_ua(self) -> None:
        """这些接口大多校验来源页，缺 Referer 会被拒 —— 而被拒的响应很像"失效"。"""
        seen: dict[str, str] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"code": "OK"})

        await _Probe(_client(_capture)).check(REF)
        assert seen["referer"] == "https://example.invalid/"
        assert "Chrome" in seen["user-agent"]

    async def test_caller_supplied_client_is_not_closed(self) -> None:
        """外部传进来的连接由外部管 —— 探针关掉它会让后续校验全部失败。"""
        client = _client(lambda r: httpx.Response(200, json={"code": "OK"}))
        await _Probe(client).check(REF)
        assert not client.is_closed
        await client.aclose()


class TestRegisteredProbesUseTheSkeleton:
    def test_all_probes_inherit_the_safe_default(self) -> None:
        """新增探针必须走骨架，否则那套安全兜底一条都不生效。"""
        from funflix.services.verify.registry import get_probe, supported_providers

        for provider in supported_providers():
            probe = get_probe(provider)
            assert isinstance(probe, AnonymousHttpProbe), f"{provider.value} 没走骨架"


@pytest.mark.asyncio
class TestUCReusesQuarkCodes:
    """UC 与夸克是同一套接口，业务码通用。

    实测（2026-08，pc-api.uc.cn，假分享 ID）返回
    `{"status":404,"code":41006,"message":"分享不存在"}` ——
    41006 正是夸克那边的"分享没了"码。所以 UC 直接复用 quark.classify，
    不另抄一份码表：抄一份就意味着夸克那边加了新码之后，UC 还在把它当未知响应。
    """

    async def test_gone_code_is_invalid(self) -> None:
        from funflix.services.verify.uc import UCProbe

        client = _client(
            lambda r: httpx.Response(404, json={"code": 41006, "message": "分享不存在"})
        )
        outcome = await UCProbe(client).check(REF)
        assert outcome.status is CheckStatus.INVALID

    async def test_unknown_code_is_error(self) -> None:
        from funflix.services.verify.uc import UCProbe

        client = _client(lambda r: httpx.Response(200, json={"code": 99999, "message": "新情况"}))
        outcome = await UCProbe(client).check(REF)
        assert outcome.status is CheckStatus.ERROR

    async def test_shares_the_quark_code_table(self) -> None:
        """两者用的是同一个 classify 函数对象，不是两份内容相同的副本。"""
        from funflix.services.verify import quark, uc

        assert uc.quark_classify is quark.classify
