"""表格行抽取器。

表格源（腾讯文档等）的每一行本身就是结构化的：剧名一列、链接若干列，
归属关系明确写在那儿，**不需要模型去猜，也不需要分段启发式**。

因此它比 RuleExtractor 更准（年份直接读「年份」列，而不是从文本里正则猜），
也比 LLM 抽取器便宜无穷倍 —— 十万行表格逐行调模型是纯粹的浪费。

输入约定是采集层渲染的 `列名：值` 格式，见 collect/tencent_docs.py 的 render_row。
"""

from __future__ import annotations

import re
import time
from typing import Any

from funflix.base.enums import MediaType, Quality
from funflix.services.extract.base import ExtractedItem, ExtractionOutcome
from funflix.services.text.linkscan import scan_links
from funflix.services.text.normalize import (
    clean_title,
    extract_episode_info,
    extract_quality,
    extract_year,
    guess_media_type,
    looks_like_catalog,
    norm_key,
)

VERSION = "v1"

#: `列名：值`。冒号后允许为空，空值行直接忽略。
_FIELD_LINE_RE = re.compile(r"^\s*(?P<label>[^：:]{1,20})\s*[：:]\s*(?P<value>.*)$")

#: 列名 → 语义。表格作者的叫法五花八门，这里收敛。
_TITLE_LABELS = frozenset(
    {"剧名", "名称", "片名", "标题", "资源名", "资源名称", "影片名", "电影名"}
)
_YEAR_LABELS = frozenset({"年份", "年代", "上映年份", "首播年份"})
_TYPE_LABELS = frozenset({"类型", "分类", "题材"})
_EPISODE_LABELS = frozenset({"集数", "更新", "进度", "更新进度"})
_QUALITY_LABELS = frozenset({"画质", "清晰度", "分辨率"})
#: 这些列的值只是备注，不参与标题与类型判断
_NOTE_LABELS = frozenset(
    {"备注", "失效留言", "序号", "整理日期", "更新日期", "说明", "网盘", "来源"}
)

#: 网盘列名。这些列装的是链接，不能被当成标题。
_LINK_LABELS = frozenset(
    {"夸克", "百度", "阿里", "阿里云盘", "UC", "uc", "天翼", "115", "迅雷", "原链接", "链接"}
)

#: 未命名列的默认名。腾讯文档里作者没起名的列就叫「文本 1」「文本 2」。
#: 它们照样可能装着剧名 —— 靠列名认不出来，只能靠"是不是唯一的纯文本列"来兜底。
_UNNAMED_LABEL_RE = re.compile(r"^(?:文本|列|字段|Column|Field)\s*\d*$", re.IGNORECASE)


def parse_fields(content: str) -> dict[str, str]:
    """把 `列名：值` 文本解析成字典。同名列后出现的覆盖先出现的。"""
    fields: dict[str, str] = {}
    for line in content.splitlines():
        match = _FIELD_LINE_RE.match(line)
        if not match:
            continue
        value = match.group("value").strip()
        if value:
            fields[match.group("label").strip()] = value
    return fields


def _first(fields: dict[str, str], labels: frozenset[str]) -> str | None:
    for label, value in fields.items():
        if label in labels:
            return value
    return None


def _looks_like_link(value: str) -> bool:
    return value.lower().startswith(("http://", "https://", "magnet:"))


def find_title(fields: dict[str, str]) -> str | None:
    """找出标题列。

    先按已知列名匹配；找不到则回落到「第一个非链接、非备注的文本列」——
    同一个文档里不同 sheet 的列布局并不一致，有的 sheet 的标题列
    压根没起名（叫「文本 1」）。写死列名表在单 sheet 上够用，
    在真实的多 sheet 文档上会让整行归属失败。
    """
    known = _first(fields, _TITLE_LABELS)
    if known:
        return known

    for label, value in fields.items():
        if label in _LINK_LABELS or label in _NOTE_LABELS:
            continue
        if label in _YEAR_LABELS or label in _QUALITY_LABELS or label in _EPISODE_LABELS:
            continue
        if _looks_like_link(value):
            continue
        # 未命名列或作者自定义的列名，只要值不是链接就可以当标题候选
        return value
    return None


class SheetExtractor:
    """按列直接映射，零猜测、零 token。"""

    name = "sheet"
    version = VERSION

    async def extract(self, content: str) -> ExtractionOutcome:
        return self._build(content)

    def rehydrate(self, payload: dict[str, Any], content: str) -> ExtractionOutcome:
        """确定性且免费，直接重算。"""
        return self._build(content)

    def _build(self, content: str) -> ExtractionOutcome:
        started = time.monotonic()
        fields = parse_fields(content)
        links = scan_links(content)

        raw_title = find_title(fields)
        title = clean_title(raw_title) if raw_title else ""
        key = norm_key(title) if title else ""

        stats: dict[str, Any] = {
            "fields_parsed": len(fields),
            "links_total": len(links),
            "title_found": bool(key),
        }

        if raw_title and looks_like_catalog(raw_title):
            # 「08月03日丨短剧更新28部」这类行不是一部作品，
            # 当成作品会在 media 表里堆出大量日期式假条目。
            stats["is_catalog"] = True
            stats["links_attributed"] = 0
            stats["links_unattributed"] = len(links)
            stats["items_kept"] = 0
            return ExtractionOutcome(
                is_catalog=True,
                items=[],
                unattributed_links=links,
                all_links=links,
                extractor_name=self.name,
                extractor_version=self.version,
                raw_payload={"fields": fields},
                stats=stats,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        if not key:
            # 没有剧名列就无从归属。链接不丢，全部记为未归属，
            # 落库后 media_id=None，进人工队列。
            stats["links_attributed"] = 0
            stats["links_unattributed"] = len(links)
            stats["items_kept"] = 0
            return ExtractionOutcome(
                items=[],
                unattributed_links=links,
                all_links=links,
                extractor_name=self.name,
                extractor_version=self.version,
                raw_payload={"fields": fields},
                stats=stats,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        # 年份优先读「年份」列；该列缺失或不是年份时才回落到全文正则
        year_text = _first(fields, _YEAR_LABELS)
        year = extract_year(year_text) if year_text else None
        if year is None and raw_title:
            year = extract_year(raw_title)

        type_text = _first(fields, _TYPE_LABELS)
        media_type = MediaType.UNKNOWN
        for candidate in (type_text, raw_title):
            if candidate:
                media_type = guess_media_type(candidate)
                if media_type is not MediaType.UNKNOWN:
                    break

        episode_text = _first(fields, _EPISODE_LABELS) or raw_title or ""
        quality_text = _first(fields, _QUALITY_LABELS) or raw_title or ""

        item = ExtractedItem(
            title=title,
            norm_key=key,
            year=year,
            media_type=media_type,
            episode_info=extract_episode_info(episode_text),
            quality=extract_quality(quality_text) if quality_text else Quality.UNKNOWN,
            # 一行就是一部作品 —— 行内所有链接都归它，不存在归属歧义
            links=links,
        )

        stats["links_attributed"] = len(links)
        stats["links_unattributed"] = 0
        stats["items_kept"] = 1

        return ExtractionOutcome(
            items=[item],
            unattributed_links=[],
            all_links=links,
            extractor_name=self.name,
            extractor_version=self.version,
            raw_payload={"fields": fields},
            stats=stats,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
