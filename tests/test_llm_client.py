"""LLM 客户端的失败收口。

`extract()` 的契约是「调用失败一律抛 `LLMCallError`」，调用方
（`canon/resolver.py`、`extract/runner.py`）只接这一种。漏出去一个 openai 的
`APIError` 子类，整条命令就会退出 1，连已经调完的块也不会落库 —— 生产上就是
这么丢掉一整轮 token 的，见 `client.extract` 里那段注释。
"""

from __future__ import annotations

from typing import Any

import pytest

from funflix.services.extract.llm.client import LLMCallError, OpenAICompatClient

#: `llm` 是可选 extra，没装 openai 时整个文件都没有意义（`extract()` 里要 import 它）
openai = pytest.importorskip("openai")


class _Exploding:
    """假的 SDK 客户端：`chat.completions.create` 一调用就抛给定异常。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def chat(self) -> _Exploding:
        return self

    @property
    def completions(self) -> _Exploding:
        return self

    async def create(self, **kwargs: Any) -> Any:
        raise self._exc


def _client(exc: BaseException) -> OpenAICompatClient:
    """构造一个凭证全部显式给定（不碰 funsecret）、底层已被替换成炸弹的客户端。"""
    client = OpenAICompatClient(base_url="https://gateway.invalid/v1", api_key="k", model="m")
    client._client = _Exploding(exc)
    return client


class TestGatewayFailuresAreWrapped:
    async def test_api_error_becomes_llm_call_error(self) -> None:
        # 刻意不构造真的 `RateLimitError` —— 它要求传一个 SDK 版本相关的
        # response 对象（openai 3.x 换到了 httpx2），测试不该绑在那上面。
        # 这里要钉住的是「`APIError` 的任何子类都得被收口」。
        class _FakeRateLimit(openai.APIError):
            def __init__(self) -> None:
                Exception.__init__(self, "429 Request Rate Reaches Maximum Limit")

        with pytest.raises(LLMCallError) as excinfo:
            await _client(_FakeRateLimit()).extract("sys", "user")

        # 原始异常类型要留在消息里，否则排查时看不出是限流还是超时
        assert "_FakeRateLimit" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, openai.APIError)

    async def test_unrelated_exception_still_propagates(self) -> None:
        # 只收口网关错误。程序 bug（比如 schema 构造错了）必须照原样炸出来，
        # 否则会被 resolver 当成「这一页失败了，下次重跑」而无限重试。
        with pytest.raises(TypeError):
            await _client(TypeError("schema 不是 dict")).extract("sys", "user")
