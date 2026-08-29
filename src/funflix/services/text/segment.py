"""文本分段：把一条原始文本切成「一部作品 + 它的若干链接」。

这是**多作品 / 多链接**场景的确定性基线。一条分享文案可能是：

    名称：剧集A            ← 一部作品配一个链接
    夸克：https://...
    名称：剧集B            ← 同一条文本里的第二部作品
    夸克：https://...
    阿里：https://...      ← 同一部作品的第二个网盘

最终归属由 LLM 判定（M3），但这里的结果既是 LLM 失败时的兜底，
也是校验 LLM 有没有漏掉作品/链接的对照基准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from funflix.services.text.linkscan import ScannedLink, scan_links
from funflix.services.text.normalize import strip_title_marker

#: 强标记：`名称：xxx`。出现即可信，单独一个也足以分段。
_STRONG_ANCHOR_RE = re.compile(
    r"^\s*(?:\d+\s*[.、)）]\s*)?(?:名称|片名|剧名|标题|资源名称|资源名|影片名称|影片名"
    r"|电影名|剧集名|番名|title|name)\s*[:：]\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)

#: 弱标记：`【xxx】` 独占一行，或 markdown 标题
_HEADING_ANCHOR_RE = re.compile(r"^\s*(?:#{1,4}\s*)?[【\[]\s*(?P<title>[^】\]]{1,60})\s*[】\]]\s*$")

#: 更弱：`1. xxx` / `1、xxx` 编号行
_NUMBERED_ANCHOR_RE = re.compile(r"^\s*\d{1,2}\s*[.、)）]\s*(?P<title>\S.{0,60})\s*$")


@dataclass(slots=True)
class Segment:
    """一个分段：一部作品及归属于它的链接。"""

    title_raw: str | None
    text: str
    start: int
    end: int
    links: list[ScannedLink] = field(default_factory=list)


@dataclass(slots=True)
class SegmentedText:
    segments: list[Segment]
    #: 出现在第一个标题之前、无法归属的链接。不丢弃，交给 LLM / 人工处理。
    unattributed_links: list[ScannedLink]
    #: 原文中扫到的全部链接，供校验 LLM 有无遗漏
    all_links: list[ScannedLink]

    @property
    def attributed_count(self) -> int:
        return sum(len(s.links) for s in self.segments)


@dataclass(slots=True, frozen=True)
class _Anchor:
    offset: int
    title: str


def _line_offsets(text: str) -> list[tuple[int, str]]:
    offsets: list[tuple[int, str]] = []
    pos = 0
    for line in text.split("\n"):
        offsets.append((pos, line))
        pos += len(line) + 1
    return offsets


def _find_anchors(lines: list[tuple[int, str]], pattern: re.Pattern[str]) -> list[_Anchor]:
    anchors: list[_Anchor] = []
    for offset, line in lines:
        m = pattern.match(line)
        if m:
            title = m.group("title").strip()
            if title:
                anchors.append(_Anchor(offset=offset, title=strip_title_marker(title)))
    return anchors


def _blank_line_anchors(lines: list[tuple[int, str]]) -> list[_Anchor]:
    """空行分段：每个段落的首行当作标题候选。"""
    anchors: list[_Anchor] = []
    start_of_block = True
    for offset, line in lines:
        if not line.strip():
            start_of_block = True
            continue
        if start_of_block:
            anchors.append(_Anchor(offset=offset, title=strip_title_marker(line.strip())))
            start_of_block = False
    return anchors


def _line_has_link(offset: int, line: str, links: list[ScannedLink]) -> bool:
    end = offset + len(line)
    return any(offset <= link.start < end for link in links)


def _title_from_line(line: str) -> str:
    m = _NUMBERED_ANCHOR_RE.match(line)
    if m:
        return strip_title_marker(m.group("title").strip())
    return strip_title_marker(line.strip())


def _adjacent_link_anchors(lines: list[tuple[int, str]], links: list[ScannedLink]) -> list[_Anchor]:
    """标题另起一行、链接紧跟其后的排版 —— 没有编号/方括号/空行分隔。

    真实语料里常见一条公告混排多种格式：一部分作品用 `【标题】` 独占行，
    另一部分只是"标题行 + 紧接着的链接行"，中间连空行都没有。只认其中一种
    格式会让另一种格式的整段内容找不到锚点、连着链接一起被丢进 unattributed。
    """
    anchors: list[_Anchor] = []
    for index, (offset, line) in enumerate(lines):
        text = line.strip()
        if not text or _line_has_link(offset, line, links):
            continue
        # "夸克：" "年份：oMUsGC" "整理日期：xxx" 这类字段行是给同一个作品打元数据
        # 标签的，不是标题 —— 真实标题基本不含裸冒号，用这点把两者分开，
        # 否则会把同一部作品的多个元数据字段行拆成好几个假标题。
        if ":" in text or "：" in text:
            continue
        for next_offset, next_line in lines[index + 1 :]:
            if not next_line.strip():
                continue
            if _line_has_link(next_offset, next_line, links):
                anchors.append(_Anchor(offset=offset, title=_title_from_line(line)))
            break
    return anchors


def _build_segments(text: str, anchors: list[_Anchor], links: list[ScannedLink]) -> list[Segment]:
    """按锚点切段，并把链接按字符区间归属到所在段。"""
    segments: list[Segment] = []
    for index, anchor in enumerate(anchors):
        end = anchors[index + 1].offset if index + 1 < len(anchors) else len(text)
        segments.append(
            Segment(
                title_raw=anchor.title,
                text=text[anchor.offset : end].strip(),
                start=anchor.offset,
                end=end,
            )
        )

    for link in links:
        for segment in segments:
            if segment.start <= link.start < segment.end:
                segment.links.append(link)
                break
    return segments


def _segments_with_links(segments: list[Segment]) -> int:
    return sum(1 for s in segments if s.links)


def segment_text(text: str) -> SegmentedText:
    """把一条原始文本切成若干「作品 + 链接」分段。

    分段策略从强到弱依次尝试，第一个站得住的胜出：

    1. `名称：` 等强标记 —— 语义明确，出现一次就够。
    2. `【标题】` 独占行 / 标题行紧跟链接行（无编号、无空行分隔）—— 合并成一组
       一起尝试，因为真实语料常见一条公告里混排这两种排版，只认其中一种会让
       另一种整段找不到锚点、连着链接一起被丢进 unattributed。
    3. `1. xxx` 编号行。
    4. 空行分隔的段落。

    弱策略（2-4）额外要求**至少两个分段各自带链接**才被采纳。
    否则就说明这些行是简介里的排版（比如描述字段里的 `1. 穿书 / 2. 复仇`），
    不是作品边界 —— 强行按它切会把链接归属到错误的标题上。
    """
    links = scan_links(text)
    lines = _line_offsets(text)

    strong = _find_anchors(lines, _STRONG_ANCHOR_RE)
    if strong:
        segments = _build_segments(text, strong, links)
    else:
        segments = []
        # 同一 offset 上两种模式都命中时，标题保留方括号内文本的干净版本
        by_offset = {a.offset: a for a in _adjacent_link_anchors(lines, links)}
        by_offset.update({a.offset: a for a in _find_anchors(lines, _HEADING_ANCHOR_RE)})
        heading_like = sorted(by_offset.values(), key=lambda a: a.offset)

        for pattern_anchors in (
            heading_like,
            _find_anchors(lines, _NUMBERED_ANCHOR_RE),
            _blank_line_anchors(lines),
        ):
            if not pattern_anchors:
                continue
            candidate = _build_segments(text, pattern_anchors, links)
            if _segments_with_links(candidate) >= 2:
                segments = candidate
                break

        if not segments:
            # 兜底：整条文本算一部作品，标题取首个非空行
            first_line = next((line.strip() for _, line in lines if line.strip()), "")
            segments = _build_segments(
                text, [_Anchor(offset=0, title=strip_title_marker(first_line))], links
            )

    attributed = {id(link) for segment in segments for link in segment.links}
    unattributed = [link for link in links if id(link) not in attributed]

    return SegmentedText(segments=segments, unattributed_links=unattributed, all_links=links)
