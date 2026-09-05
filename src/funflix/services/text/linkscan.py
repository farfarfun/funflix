"""网盘链接扫描。

确定性的正则实现，是 LLM 抽取结果的**校验基准**（见 docs/DESIGN.md §4.2）：
LLM 负责判断"这个链接属于哪部作品"，但"原文里到底有哪些链接"必须由这里说了算，
否则 LLM 幻觉出的 URL 会直接变成脏数据。

一条文本里可能有任意多个链接，`scan_links` 永远返回全部，不做截断。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from funflix.base.enums import Provider

#: 从文本里粗提 URL。刻意不含中文标点与空白，避免把后面的中文吞进来。
_URL_RE = re.compile(
    r"(?:https?://[^\s<>\"'，。、；：！？（）【】《》「」『』]+"
    r"|magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\s]*"
    r"|ed2k://\|file\|[^\r\n<>\"']+?\|/)",
    re.IGNORECASE,
)

#: URL 尾部需要剥掉的标点。中文文案里 `链接：https://x.com/s/abc。` 极常见，
#: 不剥的话 share_id 会多带一个句号，去重和校验全部失准。
_TRAILING_PUNCT = "。，、；：！？）】》」』…,.;:!?)]}>\"'　 "

#: 各网盘的分享链接形态。新增网盘 = 在这里加一行。
#: 顺序有意义：先匹配到的先算，故更具体的模式要排在更宽松的前面。
_PROVIDER_PATTERNS: tuple[tuple[Provider, re.Pattern[str]], ...] = (
    (Provider.QUARK, re.compile(r"^https?://pan\.quark\.cn/s/(?P<sid>[A-Za-z0-9]+)", re.I)),
    (Provider.UC, re.compile(r"^https?://drive\.uc\.cn/s/(?P<sid>[A-Za-z0-9]+)", re.I)),
    (
        Provider.ALIPAN,
        re.compile(
            r"^https?://(?:www\.)?(?:alipan|aliyundrive)\.com/(?:s|t)/(?P<sid>[A-Za-z0-9]+)", re.I
        ),
    ),
    (
        Provider.BAIDU,
        re.compile(
            r"^https?://(?:pan|yun)\.baidu\.com/(?:s/1(?P<sid>[A-Za-z0-9_\-]+)"
            r"|share/init\?surl=(?P<sid2>[A-Za-z0-9_\-]+))",
            re.I,
        ),
    ),
    (
        Provider.PAN115,
        re.compile(r"^https?://(?:115|115cdn|anxia)\.com/s/(?P<sid>[A-Za-z0-9]+)", re.I),
    ),
    (
        Provider.PAN123,
        re.compile(r"^https?://(?:www\.)?123(?:pan|\d+)\.com/s/(?P<sid>[A-Za-z0-9_-]+)", re.I),
    ),
    (
        Provider.MOBILE139,
        re.compile(r"^https?://yun\.139\.com/shareweb/#/w/i/(?P<sid>[A-Za-z0-9_-]+)", re.I),
    ),
    (
        Provider.GUANGYA,
        re.compile(r"^https?://(?:www\.)?guangyapan\.com/s/(?P<sid>[A-Za-z0-9_-]+)", re.I),
    ),
    (Provider.TIANYI, re.compile(r"^https?://cloud\.189\.cn/t/(?P<sid>[A-Za-z0-9]+)", re.I)),
    (Provider.XUNLEI, re.compile(r"^https?://pan\.xunlei\.com/s/(?P<sid>[A-Za-z0-9_\-]+)", re.I)),
    (
        Provider.LANZOU,
        re.compile(r"^https?://(?:[a-z0-9\-]+\.)?lanzou[a-z]*\.com/(?P<sid>[A-Za-z0-9_\-]+)", re.I),
    ),
    (Provider.MAGNET, re.compile(r"^magnet:\?xt=urn:btih:(?P<sid>[A-Za-z0-9]+)", re.I)),
    (
        Provider.ED2K,
        re.compile(r"^ed2k://\|file\|.*?\|\d+\|(?P<sid>[A-Fa-f0-9]{32})\|", re.I),
    ),
)

#: URL 自带的提取码参数
_PWD_IN_URL_RE = re.compile(r"[?&](?:pwd|password|passcode)=(?P<pwd>[A-Za-z0-9]{4,8})", re.I)

#: 链接附近的提取码文案。4-8 位字母数字是各网盘的通行长度。
_PWD_NEAR_RE = re.compile(
    r"(?:提取码|提取密码|访问码|密码|口令|pwd|code)\s*[:：=]?\s*(?P<pwd>[A-Za-z0-9]{4,8})",
    re.IGNORECASE,
)

#: 在链接后多远的范围内找提取码。跨一行足够，跨太多会串到下一条资源上。
_PWD_LOOKAHEAD = 60
_MAX_SHARE_ID_LENGTH = 255
_MAX_URL_LENGTH = 2048


@dataclass(frozen=True, slots=True)
class ScannedLink:
    """原文中的一条链接。`start`/`end` 是在原文里的字符区间，用于归属到分段。"""

    provider: Provider
    share_id: str
    url: str
    raw_url: str
    passcode: str | None
    start: int
    end: int

    @property
    def key(self) -> tuple[Provider, str]:
        """全局去重锚点，与 resource 表的唯一约束一致。"""
        return (self.provider, self.share_id)


def _trim_url(url: str) -> str:
    """剥掉尾部标点。

    右括号要小心：`（链接 https://x.com/s/abc）` 里的 `)` 是文案的，
    但 URL 本身也可能合法含括号。这里只在括号不成对时才剥。
    """
    trimmed = url.rstrip(_TRAILING_PUNCT)
    while trimmed and trimmed[-1] in ")]}":
        opener = {")": "(", "]": "[", "}": "{"}[trimmed[-1]]
        if trimmed.count(opener) >= trimmed.count(trimmed[-1]):
            break  # 括号成对，是 URL 的一部分
        trimmed = trimmed[:-1].rstrip(_TRAILING_PUNCT)
    return trimmed


def identify_provider(url: str) -> tuple[Provider, str] | None:
    """识别 URL 属于哪个网盘并提取 share_id；无法识别返回 None。"""
    for provider, pattern in _PROVIDER_PATTERNS:
        m = pattern.match(url)
        if m:
            groups = m.groupdict()
            sid = groups.get("sid") or groups.get("sid2")
            if sid:
                return provider, sid.lower() if provider is Provider.ED2K else sid
    return None


def _find_passcode(text: str, link_end: int, url: str) -> str | None:
    """先看 URL 自带参数，再看链接后方文案。"""
    in_url = _PWD_IN_URL_RE.search(url)
    if in_url:
        return in_url.group("pwd")
    window = text[link_end : link_end + _PWD_LOOKAHEAD]
    near = _PWD_NEAR_RE.search(window)
    return near.group("pwd") if near else None


def scan_links(text: str, *, include_unknown: bool = True) -> list[ScannedLink]:
    """扫出文本中**全部**网盘链接，按出现顺序返回。

    同一个 (provider, share_id) 在一条文本里重复出现时只保留首次 ——
    分享文案常把同一个链接写两遍（正文一次、末尾再贴一次）。

    Args:
        include_unknown: 是否保留无法识别网盘的 http 链接（记为 Provider.OTHER）。
            这些链接大多是频道自宣、短链，但也可能是尚未支持的网盘，默认保留待人工看。
    """
    results: list[ScannedLink] = []
    seen: set[tuple[Provider, str]] = set()

    for match in _URL_RE.finditer(text):
        raw_url = match.group(0)
        matched_url = _trim_url(raw_url)
        if not matched_url:
            continue

        identified = identify_provider(matched_url)
        if identified is None:
            if not include_unknown:
                continue
            provider = Provider.OTHER
            share_id = (
                matched_url
                if len(matched_url) <= _MAX_SHARE_ID_LENGTH
                else "sha256:" + hashlib.sha256(matched_url.encode()).hexdigest()
            )
        else:
            provider, share_id = identified

        end = match.start() + len(matched_url)
        url = f"magnet:?xt=urn:btih:{share_id}" if provider is Provider.MAGNET else matched_url
        if len(url) > _MAX_URL_LENGTH:
            continue

        key = (provider, share_id)
        if key in seen:
            continue
        seen.add(key)

        results.append(
            ScannedLink(
                provider=provider,
                share_id=share_id,
                url=url,
                raw_url=raw_url,
                passcode=_find_passcode(text, end, matched_url),
                start=match.start(),
                end=end,
            )
        )

    return results


def scan_known_links(text: str) -> list[ScannedLink]:
    """只返回能识别出网盘的链接。"""
    return scan_links(text, include_unknown=False)
