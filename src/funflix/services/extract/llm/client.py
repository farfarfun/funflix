"""LLM 客户端。

走 OpenAI 兼容协议（`base_url` 可配意味着可以指向任意网关），
凭证由 `funsecret` 提供，不进环境变量、不进代码、不入库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from farlog import getLogger

from funflix.services.extract.llm.prompts import TOOL_NAME, TOOL_SCHEMA

logger = getLogger("funflix")

#: funsecret 的分类路径：read_secret("funflix", "llm", <key>)
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

    async def extract(self, system: str, user: str) -> LLMResult:
        """用给定的 system/user 消息调用一次模型，返回结构化产出与用量信息。

        Args:
            system: system prompt，描述抽取任务与约束。
            user: user 消息，通常是原文 + 已扫描链接清单。

        Returns:
            `LLMResult`：模型返回的工具调用参数，连同模型名、token 用量、耗时。

        Raises:
            LLMCallError: 响应结构不合法（无 choices、未调用工具、参数非法 JSON 等）。
        """
        ...


def read_llm_secret(key: str) -> str:
    """从 funsecret 读一项 LLM 配置。

    `read_secret` 在未命中时返回 None（尽管它标注的是 `-> str`），
    这里显式转成带指引的报错 —— 否则 None 会一路传到客户端构造，
    抛出一个跟"没配凭证"毫无关系的异常。
    """
    try:
        from funsecret import read_secret
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise LLMConfigError("未安装 funsecret，无法读取 LLM 凭证") from exc

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
        tool_schema: dict[str, Any] | None = None,
        tool_name: str | None = None,
    ) -> None:
        """构造客户端，未显式传入的凭证/模型/地址从 funsecret 读取。

        底层 SDK 客户端（`AsyncOpenAI`）延迟到首次 `extract()` 调用时才创建，
        这样仅构造实例（比如被缓存但始终未被调用）不会强制要求已安装 `openai` 包。

        Args:
            base_url: OpenAI 兼容网关地址；为 None 时读取 funsecret 的 `base_url`。
            api_key: API Key；为 None 时读取 funsecret 的 `api_key`。
            model: 模型名；为 None 时读取 funsecret 的 `model`。
            timeout: 单次请求超时秒数。
            temperature: 采样温度，默认 0 以保证抽取结果尽量确定。
            max_retries: 底层 SDK 的请求失败重试次数。

        Raises:
            LLMConfigError: 对应的 funsecret 配置项未设置。
        """
        self.base_url = base_url or read_llm_secret("base_url")
        self.model = model or read_llm_secret("model")
        self._api_key = api_key or read_llm_secret("api_key")
        self._timeout = timeout
        self._temperature = temperature
        self._max_retries = max_retries
        # 工具 schema 可换，默认是抽取用的那套。归一服务（`services/canon`）
        # 要的输出结构完全不同，但协议、重试、凭证读取这些都一样 ——
        # 与其复制一个客户端，不如把 schema 变成构造参数。
        # `extract()` 的签名不动，`LLMClient` 协议因此保持原样。
        self._tool_schema = tool_schema or TOOL_SCHEMA
        self._tool_name = tool_name or TOOL_NAME
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
        """以强制工具调用的方式请求一次 chat completion，并解析出工具参数。

        `tool_choice` 被硬编码为 `TOOL_SCHEMA` 对应的函数，不给模型"用自然语言
        回答"的选项，保证返回值要么是合法的工具参数，要么在 `_extract_tool_arguments`
        里按具体失败原因抛出 `LLMCallError`。

        Args:
            system: system prompt。
            user: user 消息。

        Returns:
            `LLMResult`：解析出的工具参数 payload，连同模型名、输入/输出 token 数、耗时（毫秒）。

        Raises:
            LLMCallError: 响应中没有 choices、模型未调用工具、或工具参数不是合法 JSON 对象。
        """
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
            tools=[self._tool_schema],
            # 强制走工具，不给模型"用自然语言回答"的选项
            tool_choice={"type": "function", "function": {"name": self._tool_name}},
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
    """按 funsecret 里的配置构造客户端。凭证缺失时抛 LLMConfigError。"""
    return OpenAICompatClient()
