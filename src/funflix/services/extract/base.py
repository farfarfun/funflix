"""抽取环节的抽象。

一个 Extractor 把「一条原始文本」变成「若干作品 + 每部作品的链接」。
实现之间可互换：LLM 抽取器质量高但要花钱，规则抽取器免费且离线可跑，
两者产出同一种结构，落库与状态机逻辑（runner）完全复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from funflix.base.enums import MediaType, Quality
from funflix.services.text.linkscan import ScannedLink


@dataclass(slots=True)
class ExtractedItem:
    """一部作品及归属于它的链接。"""

    title: str
    norm_key: str
    original_title: str | None = None
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    episode_info: str | None = None
    quality: Quality = Quality.UNKNOWN
    links: list[ScannedLink] = field(default_factory=list)
    #: 分类标签，形如 [("genre", "悬疑"), ("region", "国产")]
    tags: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class ExtractionOutcome:
    """一次抽取的完整产出。"""

    is_catalog: bool = False
    items: list[ExtractedItem] = field(default_factory=list)
    #: 扫到但没归属给任何作品的链接。不丢弃，落库时挂 media_id=None。
    unattributed_links: list[ScannedLink] = field(default_factory=list)
    all_links: list[ScannedLink] = field(default_factory=list)

    #: 抽取器身份，写入 extraction.model / extraction.prompt_version，
    #: 二者共同构成缓存键 —— 换实现或升版本都会重新抽取。
    extractor_name: str = ""
    extractor_version: str = ""

    #: 原始产出留档（LLM 是 tool 参数，规则抽取器是分段摘要）
    raw_payload: dict[str, Any] = field(default_factory=dict)
    #: 质量指标，写入 extraction.stats
    stats: dict[str, Any] = field(default_factory=dict)

    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None

    @property
    def attributed_count(self) -> int:
        return sum(len(item.links) for item in self.items)


@runtime_checkable
class Extractor(Protocol):
    """抽取器接口。

    `name` 与 `version` 决定缓存键，改了抽取逻辑就必须动其中之一，
    否则会命中旧缓存拿到过时结果。
    """

    #: 抽取器标识。LLM 抽取器用模型名，规则抽取器用 "rule"。
    name: str
    #: 版本号。prompt 或规则有任何改动都要升。
    version: str

    async def extract(self, content: str) -> ExtractionOutcome: ...

    def rehydrate(self, payload: dict[str, Any], content: str) -> ExtractionOutcome:
        """从留档的 payload 还原产出，不重新调用外部服务。

        缓存命中时走这里 —— 所以它必须是同步且无副作用的。
        """
        ...
