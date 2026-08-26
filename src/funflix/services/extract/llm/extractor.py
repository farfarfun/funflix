"""LLM 抽取器。

核心职责是**不信任模型输出**：序号越界、标题为空、同一链接被塞给多部作品，
都在这里拦下并计入 stats，让"模型今天变笨了"能从指标上看出来，
而不是变成库里一堆脏数据。
"""

from __future__ import annotations

import logging
from typing import Any

from funflix.base.enums import MediaType, Quality
from funflix.services.extract.base import ExtractedItem, ExtractionOutcome
from funflix.services.extract.llm.client import LLMClient, LLMResult, build_default_client
from funflix.services.extract.llm.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_message
from funflix.services.text.linkscan import ScannedLink, scan_links
from funflix.services.text.normalize import clean_title, extract_tags, norm_key

logger = logging.getLogger(__name__)

#: 年份的合理区间，超出即视为模型瞎填
_YEAR_MIN, _YEAR_MAX = 1900, 2100


def format_link_lines(links: list[ScannedLink]) -> list[str]:
    """把链接清单渲染成带序号的行，喂给模型。"""
    return [
        f"[{i}] {link.url}"
        + (f"  （{link.provider.value}）" if link.provider else "")
        + (f"  提取码 {link.passcode}" if link.passcode else "")
        for i, link in enumerate(links)
    ]


def _coerce_year(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if _YEAR_MIN <= value <= _YEAR_MAX else None


def _coerce_enum[T](raw: Any, enum_cls: type[T], default: T) -> T:
    if not isinstance(raw, str):
        return default
    try:
        return enum_cls(raw.strip().lower())  # type: ignore[call-arg]
    except ValueError:
        return default


def _optional_str(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text or None


def parse_payload(
    payload: dict[str, Any], links: list[ScannedLink], source_text: str = ""
) -> ExtractionOutcome:
    """把模型返回的原始 payload 校验成结构化结果。

    校验而非信任：模型可能给出越界序号、空标题、把同一个链接分给多部作品。
    每一种都被拦下并计数，不会静默进库。
    """
    stats: dict[str, Any] = {
        "invalid_link_index": 0,
        "duplicate_link_assignment": 0,
        "empty_title_dropped": 0,
        "items_returned": 0,
    }

    is_catalog = bool(payload.get("is_catalog"))
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    stats["items_returned"] = len(raw_items)

    items: list[ExtractedItem] = []
    claimed: set[int] = set()

    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            stats["empty_title_dropped"] += 1
            continue

        raw_title = str(raw_item.get("title") or "").strip()
        # 模型给的标题再过一遍确定性清洗：它对噪声的剥离并不稳定
        title = clean_title(raw_title) or raw_title
        key = norm_key(title)
        if not title or not key:
            stats["empty_title_dropped"] += 1
            continue

        item_links: list[ScannedLink] = []
        for index in raw_item.get("link_indexes") or []:
            if not isinstance(index, int) or isinstance(index, bool):
                stats["invalid_link_index"] += 1
                continue
            if not 0 <= index < len(links):
                # 序号越界 —— 模型编了一个不存在的链接
                stats["invalid_link_index"] += 1
                continue
            if index in claimed:
                # 同一个链接被分给了多部作品，只认第一部
                stats["duplicate_link_assignment"] += 1
                continue
            claimed.add(index)
            item_links.append(links[index])

        items.append(
            ExtractedItem(
                title=title,
                norm_key=key,
                original_title=_optional_str(raw_item.get("original_title")),
                year=_coerce_year(raw_item.get("year")),
                media_type=_coerce_enum(raw_item.get("media_type"), MediaType, MediaType.UNKNOWN),
                episode_info=_optional_str(raw_item.get("episode_info")),
                quality=_coerce_enum(raw_item.get("quality"), Quality, Quality.UNKNOWN),
                links=item_links,
            )
        )

    # 井号标签由确定性函数从原文抽，不问 LLM —— `#悬疑` 是作者的明确标注，
    # 正则拿得又准又便宜，没必要花 token 让模型转述一遍。
    #
    # 只在**整条文本恰好对应一部作品**时才挂上去。一条文本含多部作品时，
    # 无从知道某个标签属于哪一部，全挂等于把恐怖片的标签安到喜剧上，
    # 污染的是筛选导航 —— 宁可少标也不错标（与 extract_tags 的取舍一致）。
    if source_text and len(items) == 1:
        items[0].tags = extract_tags(source_text)
        stats["tags_attached"] = len(items[0].tags)
    elif len(items) > 1:
        stats["tags_skipped_multi_item"] = True

    unattributed = [link for i, link in enumerate(links) if i not in claimed]
    stats["links_total"] = len(links)
    stats["links_attributed"] = len(claimed)
    stats["links_unattributed"] = len(unattributed)
    stats["items_kept"] = len(items)

    return ExtractionOutcome(
        is_catalog=is_catalog,
        items=items,
        unattributed_links=unattributed,
        all_links=links,
        raw_payload=payload,
        stats=stats,
        extractor_version=PROMPT_VERSION,
    )


class LLMExtractor:
    """调用 LLM 完成抽取。

    链接先由确定性正则扫出来，作为「已知事实」连同原文一起给模型；
    模型只能按序号引用，**在结构上就无法幻觉出一个不存在的 URL**。
    """

    version = PROMPT_VERSION

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or build_default_client()

    @property
    def name(self) -> str:
        """抽取器标识用模型名 —— 换模型即换缓存键，会重新抽取。"""
        return self._client.model

    def rehydrate(self, payload: dict[str, Any], content: str) -> ExtractionOutcome:
        """从 extraction.output 还原，不再调用模型。"""
        outcome = parse_payload(payload, scan_links(content), content)
        outcome.extractor_name = self.name
        return outcome

    async def extract(self, content: str) -> ExtractionOutcome:
        links = scan_links(content)
        user_message = build_user_message(content, format_link_lines(links))

        result: LLMResult = await self._client.extract(SYSTEM_PROMPT, user_message)
        outcome = parse_payload(result.payload, links, content)
        outcome.extractor_name = result.model
        outcome.extractor_version = PROMPT_VERSION
        outcome.input_tokens = result.input_tokens
        outcome.output_tokens = result.output_tokens
        outcome.latency_ms = result.latency_ms

        if outcome.stats["invalid_link_index"]:
            logger.warning(
                "模型给出了 %d 个越界链接序号，已丢弃", outcome.stats["invalid_link_index"]
            )
        return outcome
