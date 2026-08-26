"""LLM 客户端。

走 OpenAI 兼容协议（`base_url` 可配意味着可以指向任意网关），
凭证由 `nltsecret` 提供，不进环境变量、不进代码、不入库。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from funflix.services.extract.llm.prompts import TOOL_NAME, TOOL_SCHEMA

logger = logging.getLogger(__name__)

#: nltsecret 的分类路径：read_secret("funflix", "llm", <key>)
SECRET_CATE1 = "funflix"
SECRET_CATE2 = "llm"


class LLMConfigError(RuntimeError):
    """凭证或模型未配置。"""


class LLMCallError(RuntimeError):
    """调用失败或返回结构不合法。"""


@dataclass(slots=True)
class LLMResult:
    """一次抽取调用的产出。"""

    payload: dict[str, Any]
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


@runtime_checkable
class LLMClient(Protocol):
    """抽取器只依赖这个接口，测试用桩实现替换，不触碰真实凭证与网络。"""

    model: str

    async def extract(self, system: str, user: str) -> LLMResult: ...


def read_llm_secret(key: str) -> str:
    """从 nltsecret 读一项 LLM 配置。

    `read_secret` 在未命中时返回 None（尽管它标注的是 `-> str`），
    这里显式转成带指引的报错 —— 否则 None 会一路传到客户端构造，
    抛出一个跟"没配凭证"毫无关系的异常。
    """
    try:
        from nltsecret import read_secret
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise LLMConfigError("未安装 nltsecret，无法读取 LLM 凭证") from exc

    value = read_secret(SECRET_CATE1, SECRET_CATE2, key)
    if not value:
        raise LLMConfigError(
            f"LLM 配置项 {key!r} 未设置。请先写入："
            f'write_secret("{SECRET_CATE1}", "{SECRET_CATE2}", "{key}", value=...)'
        )
    return value


class OpenAICompatClient:
    """OpenAI 兼容协议的客户端。

    用 tool calling 而不是 response_format —— 前者几乎所有兼容网关都支持，
    后者在部分中转/开源模型上会直接报错。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        temperature: float = 0.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url or read_llm_secret("base_url")
        self.model = model or read_llm_secret("model")
        self._api_key = api_key or read_llm_secret("api_key")
        self._timeout = timeout
        self._temperature = temperature
        self._max_retries = max_retries
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - 环境问题
                raise LLMConfigError("未安装 openai，请 pip install 'funflix[llm]'") from exc
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self._api_key,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        return self._client

    async def extract(self, system: str, user: str) -> LLMResult:
        import time

        client = self._ensure_client()
        started = time.monotonic()
        response = await client.chat.completions.create(
            model=self.model,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=[TOOL_SCHEMA],
            # 强制走工具，不给模型"用自然语言回答"的选项
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        payload = _extract_tool_arguments(response)
        usage = getattr(response, "usage", None)
        return LLMResult(
            payload=payload,
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=latency_ms,
        )


def _extract_tool_arguments(response: Any) -> dict[str, Any]:
    """从 chat completion 里取出工具调用参数。

    这里的每一层缺失都对应一种真实的网关行为差异（有的返回空 choices、
    有的忽略 tool_choice 直接回文本），所以逐层报清楚是哪一步断的。
    """
    choices = getattr(response, "choices", None)
    if not choices:
        raise LLMCallError("模型返回中没有 choices")

    message = choices[0].message
    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        content = (getattr(message, "content", None) or "")[:200]
        raise LLMCallError(
            f"模型未调用工具（网关可能忽略了 tool_choice）。返回文本片段：{content!r}"
        )

    raw_args = tool_calls[0].function.arguments
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        raise LLMCallError(f"工具参数不是合法 JSON：{raw_args[:200]!r}") from exc

    if not isinstance(parsed, dict):
        raise LLMCallError(f"工具参数应为对象，实际是 {type(parsed).__name__}")
    return parsed


def build_default_client() -> OpenAICompatClient:
    """按 nltsecret 里的配置构造客户端。凭证缺失时抛 LLMConfigError。"""
    return OpenAICompatClient()
