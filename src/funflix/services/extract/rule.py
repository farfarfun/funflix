"""规则抽取器：完全确定性，不花钱、不联网、可离线跑。

用途有三个：
1. LLM 不可用（凭证没配、网关挂了、超预算）时的降级路径；
2. 对照基准 —— 拿它和 LLM 的产出做 diff，能看出 LLM 到底赢在哪、有没有变笨；
3. 单测里当默认抽取器，让落库与状态机的测试不依赖任何外部服务。
"""

from __future__ import annotations

import time
from typing import Any

from funflix.services.extract.base import ExtractedItem, ExtractionOutcome
from funflix.services.text.normalize import (
    clean_title,
    extract_episode_info,
    extract_quality,
    extract_size_bytes,
    extract_tags,
    extract_year,
    guess_media_type,
    looks_like_catalog,
    norm_key,
)
from funflix.services.text.segment import segment_text

VERSION = "v2"


def _looks_like_catalog(title: str, segment_count: int, link_count: int) -> bool:
    """判定目录帖。

    两个信号：标题本身像目录，或者「标题数远多于链接数」——
    后者对应"正文罗列几十部片名、底下只给一个总链接"的合集帖。

    标题特征走 `normalize.looks_like_catalog`，这里不再自备一份正则。
    曾经两份各自演化过：这边漏了「更新N部」、日期只认「N月N日」而不认
    斜杠/连字符/「号」，也没做全角归一。结果是同一条文本 rule 判成作品、
    sheet 判成目录 —— 走哪个抽取器决定了落出什么数据，而且不会有任何报错，
    media 表里会慢慢堆满以日期为名的假作品。
    """
    if looks_like_catalog(title):
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
                    size_bytes=extract_size_bytes(segment.text),
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
