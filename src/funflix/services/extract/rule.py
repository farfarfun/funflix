"""规则抽取器：完全确定性，不花钱、不联网、可离线跑。

用途有三个：
1. LLM 不可用（凭证没配、网关挂了、超预算）时的降级路径；
2. 对照基准 —— 拿它和 LLM 的产出做 diff，能看出 LLM 到底赢在哪、有没有变笨；
3. 单测里当默认抽取器，让落库与状态机的测试不依赖任何外部服务。
"""

from __future__ import annotations

import re
import time
from typing import Any

from funflix.services.extract.base import ExtractedItem, ExtractionOutcome
from funflix.services.text.normalize import (
    clean_title,
    extract_episode_info,
    extract_quality,
    extract_tags,
    extract_year,
    guess_media_type,
    norm_key,
)
from funflix.services.text.segment import segment_text

VERSION = "v1"

#: 目录帖/合集帖的标题特征。命中即认为这条文本不代表某一部具体作品。
_CATALOG_TITLE_RE = re.compile(
    r"目录|合集|打包|合辑|片单|清单|资源包|更新列表|\d{1,2}\s*月\s*\d{1,2}\s*日"
)


def _looks_like_catalog(title: str, segment_count: int, link_count: int) -> bool:
    """判定目录帖。

    两个信号：标题本身像目录，或者「标题数远多于链接数」——
    后者对应"正文罗列几十部片名、底下只给一个总链接"的合集帖。
    """
    if _CATALOG_TITLE_RE.search(title):
        return True
    return segment_count >= 5 and link_count <= 1


class RuleExtractor:
    """基于分段 + 归一的确定性抽取器。"""

    name = "rule"
    version = VERSION

    async def extract(self, content: str) -> ExtractionOutcome:
        return self._build(content)

    def rehydrate(self, payload: dict[str, Any], content: str) -> ExtractionOutcome:
        """规则抽取是确定性且免费的，直接重算即可，无需从 payload 还原。"""
        return self._build(content)

    def _build(self, content: str) -> ExtractionOutcome:
        started = time.monotonic()
        segmented = segment_text(content)

        items: list[ExtractedItem] = []
        catalog_votes = 0

        for segment in segmented.segments:
            raw_title = segment.title_raw or ""
            title = clean_title(raw_title)
            key = norm_key(title)
            if not title or not key:
                continue

            if _looks_like_catalog(raw_title, len(segmented.segments), len(segmented.all_links)):
                catalog_votes += 1
                continue

            items.append(
                ExtractedItem(
                    title=title,
                    norm_key=key,
                    year=extract_year(raw_title),
                    media_type=guess_media_type(segment.text, raw_title),
                    episode_info=extract_episode_info(segment.text),
                    quality=extract_quality(segment.text),
                    tags=extract_tags(segment.text),
                    links=list(segment.links),
                )
            )

        # 所有分段都像目录帖，才把整条文本判为目录帖
        is_catalog = catalog_votes > 0 and not items

        attributed = {id(link) for item in items for link in item.links}
        unattributed = [link for link in segmented.all_links if id(link) not in attributed]

        return ExtractionOutcome(
            is_catalog=is_catalog,
            items=items,
            unattributed_links=unattributed,
            all_links=segmented.all_links,
            extractor_name=self.name,
            extractor_version=self.version,
            raw_payload={
                "segments": [
                    {"title": s.title_raw, "links": [link.url for link in s.links]}
                    for s in segmented.segments
                ],
                "is_catalog": is_catalog,
            },
            stats={
                "items_kept": len(items),
                "links_total": len(segmented.all_links),
                "links_attributed": len(attributed),
                "links_unattributed": len(unattributed),
                "catalog_votes": catalog_votes,
            },
            latency_ms=int((time.monotonic() - started) * 1000),
        )
