"""采集器注册表。新增一种采集源 = 新增一个采集器并在此注册。"""

from __future__ import annotations

from funflix.base.enums import SourceType
from funflix.services.collect.base import Collector
from funflix.services.collect.telegram import TelegramChannelCollector
from funflix.services.collect.tencent_sheet import TencentSheetCollector
from funflix.services.collect.tencent_text import TencentTextCollector

_REGISTRY: dict[SourceType, type[Collector]] = {
    SourceType.TELEGRAM: TelegramChannelCollector,
    SourceType.TENCENT_DOCS: TencentSheetCollector,
    SourceType.TENCENT_DOC: TencentTextCollector,
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

    顺序有意义：先问模式更具体的采集器。Telegram 的兜底模式能匹配任意裸标识串，
    最后问才不会抢走别人的 URL。

    顺序取自各采集器自己声明的 `detect_priority`，而不是在这里手写一个类型元组。
    手写元组有两个问题：新增采集器时容易忘了加进去（于是它永远识别不出来，
    而且没有任何报错），以及顺序的**理由**离它要保护的那段正则十万八千里 ——
    谁都不知道调换两项会发生什么。现在优先级就写在模式旁边。
    """
    ordered = sorted(_REGISTRY.items(), key=lambda kv: getattr(kv[1], "detect_priority", 100))
    for source_type, cls in ordered:
        identifier = cls.normalize_identifier(url)
        if identifier:
            return source_type, identifier
    return None
