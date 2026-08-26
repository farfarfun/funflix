"""测试用的 LLM 桩客户端。

不读 funsecret、不发网络请求 —— 抽取逻辑的正确性不该依赖有没有配凭证。
"""

from __future__ import annotations

from typing import Any

from funflix.services.extract.llm.client import LLMResult


class StubLLM:
    """按预置 payload 应答的假客户端。"""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
        model: str = "stub-model",
    ) -> None:
        self.model = model
        self._payload = payload if payload is not None else {"is_catalog": False, "items": []}
        self._error = error
        self.calls = 0
        self.last_system: str | None = None
        self.last_user: str | None = None

    async def extract(self, system: str, user: str) -> LLMResult:
        self.calls += 1
        self.last_system = system
        self.last_user = user
        if self._error:
            raise self._error
        return LLMResult(
            payload=self._payload,
            model=self.model,
            input_tokens=100,
            output_tokens=50,
            latency_ms=42,
        )


def item(
    title: str,
    *,
    indexes: list[int],
    media_type: str = "tv",
    year: int | None = 2026,
    quality: str = "1080p",
    episode_info: str | None = None,
    original_title: str | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "original_title": original_title,
        "year": year,
        "media_type": media_type,
        "episode_info": episode_info,
        "quality": quality,
        "link_indexes": indexes,
    }


def payload(*items: dict[str, Any], is_catalog: bool = False) -> dict[str, Any]:
    return {"is_catalog": is_catalog, "items": list(items)}
