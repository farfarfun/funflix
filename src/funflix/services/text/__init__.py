"""文本处理原语。

不属于任何单一环节 —— 采集、抽取、校验都会用到，故独立成一层。
全部是确定性纯函数，不碰数据库和网络。
"""

from funflix.services.text.linkscan import (
    ScannedLink,
    identify_provider,
    scan_known_links,
    scan_links,
)
from funflix.services.text.normalize import (
    clean_title,
    extract_category,
    extract_episode_info,
    extract_quality,
    extract_year,
    guess_media_type,
    norm_key,
    strip_title_marker,
)
from funflix.services.text.segment import Segment, SegmentedText, segment_text

__all__ = [
    "ScannedLink",
    "Segment",
    "SegmentedText",
    "clean_title",
    "extract_category",
    "extract_episode_info",
    "extract_quality",
    "extract_year",
    "guess_media_type",
    "identify_provider",
    "norm_key",
    "scan_known_links",
    "scan_links",
    "segment_text",
    "strip_title_marker",
]
