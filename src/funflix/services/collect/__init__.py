"""采集模块对外入口：统一导出采集器协议、注册表查询与运行时触发接口。"""

from funflix.services.collect.base import CollectedMessage, Collector, FetchResult
from funflix.services.collect.registry import (
    detect_source,
    get_collector,
    get_collector_class,
    supported_source_types,
)
from funflix.services.collect.runner import CollectReport, collect_source
from funflix.services.collect.telegram import TelegramChannelCollector, parse_channel_page

__all__ = [
    "CollectReport",
    "CollectedMessage",
    "Collector",
    "FetchResult",
    "TelegramChannelCollector",
    "collect_source",
    "detect_source",
    "get_collector",
    "get_collector_class",
    "parse_channel_page",
    "supported_source_types",
]
