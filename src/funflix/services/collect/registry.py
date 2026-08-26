"""采集器注册表。新增一种采集源 = 新增一个采集器并在此注册。"""

from __future__ import annotations

from funflix.base.enums import SourceType
from funflix.services.collect.base import Collector
from funflix.services.collect.telegram import TelegramChannelCollector
from funflix.services.collect.tencent_doc import TencentDocCollector
from funflix.services.collect.tencent_docs import TencentDocsCollector

_REGISTRY: dict[SourceType, type[Collector]] = {
    SourceType.TELEGRAM: TelegramChannelCollector,
    SourceType.TENCENT_DOCS: TencentDocsCollector,
    SourceType.TENCENT_DOC: TencentDocCollector,
}


def get_collector_class(source_type: SourceType) -> type[Collector] | None:
    return _REGISTRY.get(source_type)


def get_collector(source_type: SourceType) -> Collector | None:
    cls = get_collector_class(source_type)
    return cls() if cls else None


def supported_source_types() -> list[SourceType]:
    return sorted(_REGISTRY, key=lambda s: s.value)


def detect_source(url: str) -> tuple[SourceType, str] | None:
    """从 URL 猜采集源类型与标识；无法识别时返回 None。

    顺序有意义：先问模式更具体的采集器。
    Telegram 的兜底模式能匹配任意裸标识串，放在最后才不会抢走别人的 URL。
    """
    for source_type in (SourceType.TENCENT_DOCS, SourceType.TENCENT_DOC, SourceType.TELEGRAM):
        cls = _REGISTRY.get(source_type)
        if cls is None:
            continue
        identifier = cls.normalize_identifier(url)
        if identifier:
            return source_type, identifier
    return None
