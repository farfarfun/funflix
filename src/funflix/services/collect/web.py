"""公开影视网站与论坛列表页采集器。"""

from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

import httpx

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress
from funflix.services.collect.collection import collection_text
from funflix.services.collect.rss import _text
from funflix.services.text.linkscan import scan_known_links

_DETAIL_RE = re.compile(
    r"/(?:[^/?#]+/)*\d+\.html?$|/threads/(?:[^/?#]+\.)?\d+/?$"
    r"|/d/\d+(?:-[^/?#]+)?/?$|/(?:thread|topic|post)-?\d+(?:\.html?)?$"
    r"|[?&](?:id|tid|topic|post)=\d+",
    re.I,
)
_ONCLICK_URL_RE = re.compile(
    r"(?:window\.)?location(?:\.href)?\s*=\s*(?P<quote>['\"])(?P<url>.+?)(?P=quote)", re.I
)
_CONTENT_LINK_RE = re.compile(r"(?:post|entry|article|thread|topic)[-_ ]?(?:title|link)", re.I)
_RESOURCE_LABEL_RE = re.compile(
    r"下载|网盘|磁力|torrent|\bBT\b|\b[124]K\b|1080|2160|全?第?\d+集", re.I
)
_ATTACHMENT_RE = re.compile(
    r"(?:^|/)(?:attach(?:ment)?|download|tdown|torrent|bt)(?:[-_/]|$)|\.torrent(?:$|[?#])",
    re.I,
)
_CHARSET_RE = re.compile(rb"charset\s*=\s*['\"]?([A-Za-z0-9_-]+)", re.I)
_SEEN_KEY = "web_seen_ids"
_MAX_SEEN = 2000
_MAX_TORRENT_BYTES = 5 * 1024 * 1024
_TITLE_BRACKET_RE = re.compile(r"[《【\[]([^》】\]]{2,120})[》】\]]")
_TITLE_NOISE_RE = re.compile(
    r"(?:(?:bt|夸克|百度|迅雷|网盘)(?:资源)?下载|web[-_. ]|\d+(?:\.\d+)?[gm]b|"
    r"\d{3,4}p|中文字幕|双语|最新)$",
    re.I,
)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _decode(payload: bytes) -> str:
    match = _CHARSET_RE.search(payload[:5000])
    encoding = match.group(1).decode("ascii", errors="ignore") if match else "utf-8"
    if encoding.lower() in {"gb2312", "gbk"}:
        encoding = "gb18030"
    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        """初始化标题/链接收集缓冲区，以及解析 `<a>` 标签时用到的临时状态。"""
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.content_links: set[str] = set()
        self._in_title = False
        self._href: str | None = None
        self._label: list[str] = []
        self._content_link = False
        self._bookmark = False
        self._link_title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """记录 `<title>` 开始；`<a href>` 开始时记下 href 并重置锚文本收集，
        同时依据 class/title 属性或 `rel=bookmark` 预判它是否可能是「正文内容
        链接」（详情页入口），供 `handle_endtag` 最终归类使用。
        """
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "a" and (href := values.get("href")):
            self._href = href
            self._label = []
            self._content_link = bool(
                _CONTENT_LINK_RE.search(values.get("class") or "")
                or (values.get("title") or "").strip()
            )
            self._bookmark = "bookmark" in (values.get("rel") or "").lower()
            self._link_title = values.get("title") or ""

    def handle_endtag(self, tag: str) -> None:
        """`</title>` 结束标题收集；`</a>` 结束时把 `(href, 锚文本)` 记入
        `links`，并根据起始标签阶段的判定（`content_link` 或 `bookmark` +
        资源关键词匹配锚文本）决定是否把该链接计入 `content_links`。
        """
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            label = self._link_title or "".join(self._label).strip()
            self.links.append((self._href, label))
            if self._content_link or (self._bookmark and _RESOURCE_LABEL_RE.search(label)):
                self.content_links.add(self._href)
            self._href = None
            self._label = []
            self._content_link = False
            self._bookmark = False
            self._link_title = ""

    def handle_data(self, data: str) -> None:
        """在 `<title>` 内累积标题文本；在 `<a>` 标签内累积锚文本。"""
        if self._in_title:
            self.title_parts.append(data)
        if self._href is not None:
            self._label.append(data)

    @property
    def title(self) -> str:
        """合并后的页面标题文本（连续空白折叠成单个空格）。"""
        return " ".join("".join(self.title_parts).split())


def _parse_page(value: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(value)
    parser.close()
    parser.links.extend(
        (html.unescape(match.group("url")), "") for match in _ONCLICK_URL_RE.finditer(value)
    )
    return parser


def _page_content(value: str) -> str:
    text = _text(value)
    visible_keys = {link.key for link in scan_known_links(text)}
    embedded_links = [link.url for link in scan_known_links(value) if link.key not in visible_keys]
    return "\n".join((text, *embedded_links))


def _title(value: str) -> str:
    prefix = re.split(r"[【\[]", value, maxsplit=1)[0].strip()
    if prefix and prefix != value:
        return prefix
    brackets = [match.group(1).strip() for match in _TITLE_BRACKET_RE.finditer(value)]
    if candidate := next((item for item in brackets if not _TITLE_NOISE_RE.search(item)), ""):
        return candidate
    return re.split(
        r"(?:迅雷下载|下载[,，_-]|[-_|](?:最新电影|电影下载|免费电影下载|影视下载))",
        value,
    )[0].strip()


def _bencode_string(payload: bytes, start: int) -> tuple[bytes, int]:
    colon = payload.index(b":", start)
    length = int(payload[start:colon])
    end = colon + 1 + length
    if end > len(payload):
        raise ValueError("截断的 bencode 字符串")
    return payload[colon + 1 : end], end


def _bencode_end(payload: bytes, start: int) -> int:
    token = payload[start : start + 1]
    if token.isdigit():
        return _bencode_string(payload, start)[1]
    if token == b"i":
        return payload.index(b"e", start + 1) + 1
    if token in {b"d", b"l"}:
        position = start + 1
        while payload[position : position + 1] != b"e":
            position = _bencode_end(payload, position)
        return position + 1
    raise ValueError("无效的 bencode 数据")


def _torrent_magnet(payload: bytes, name: str) -> str | None:
    if not payload.startswith(b"d") or len(payload) > _MAX_TORRENT_BYTES:
        return None
    try:
        position = 1
        while payload[position : position + 1] != b"e":
            key, position = _bencode_string(payload, position)
            value_start = position
            position = _bencode_end(payload, position)
            if key == b"info":
                digest = hashlib.sha1(payload[value_start:position]).hexdigest()  # noqa: S324
                return f"magnet:?xt=urn:btih:{digest}&dn={quote(name)}"
    except (IndexError, RecursionError, ValueError):
        return None
    return None


def _canonical_url(url: str) -> str | None:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    clean, _fragment = urldefrag(urlunsplit(parts))
    return clean if len(clean) <= 128 else "sha256:" + hashlib.sha256(clean.encode()).hexdigest()


def _detail_pattern(source: Source) -> re.Pattern[str]:
    raw = (source.extra or {}).get("detail_pattern")
    return re.compile(str(raw), re.I) if raw else _DETAIL_RE


class WebCollector(SupportsProgress):
    """公开影视网站/论坛列表页采集器。

    抓一次列表页：若列表页本身已经带资源链接（如论坛帖子页），直接把它当作
    一条消息产出；同时按 URL 模式与正文特征识别出列表里的「详情页」链接，
    逐个访问并提取其中的资源链接（含种子文件解出 magnet）。只采一轮、没有
    跨轮翻页，去重状态存在 `Source.extra[_SEEN_KEY]`。
    """

    name = "public-web-v1"
    detect_priority = 950

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """
        Args:
            client: 复用的 `httpx.AsyncClient`；为 None 时各方法自行创建并关闭。
        """
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        """把 url 规范化（去 fragment，过长则做哈希摘要）作为 identifier。

        `detect_priority` 是所有采集器里最大的，因为这里几乎接受任何合法的
        http(s) URL——只有在其他更具体的采集器都未命中时才会轮到它兜底。

        Args:
            url: 待识别的源地址。

        Returns:
            规范化后的 URL（或其哈希）；协议非 http(s) 或缺少 host 时返回 None。
        """
        return _canonical_url(url)

    async def _detail_message(
        self, client: httpx.AsyncClient, url: str, listing_title: str
    ) -> tuple[CollectedMessage | None, int]:
        response = await client.get(url, headers={"User-Agent": DEFAULT_UA})
        response.raise_for_status()
        value = _decode(response.content)
        page = _parse_page(value)
        content = _page_content(value)
        attachment_links: list[str] = []
        requests = 1
        attachments = [
            (urljoin(url, href), label)
            for href, label in page.links
            if label.lower().endswith(".torrent") or _ATTACHMENT_RE.search(urlsplit(href).path)
        ]
        for attachment_url, label in attachments[:5]:
            torrent = await client.get(attachment_url, headers={"User-Agent": DEFAULT_UA})
            torrent.raise_for_status()
            requests += 1
            if magnet := _torrent_magnet(torrent.content, label or page.title):
                attachment_links.append(magnet)
            else:
                attachment_links.extend(
                    link.url for link in scan_known_links(_decode(torrent.content))
                )

        content = "\n".join((content, *attachment_links))
        links = scan_known_links(content)
        title = _title(html.unescape(listing_title or page.title))
        if not title or not links:
            return None, requests
        normalized = collection_text(content) or f"名称：{title}\n{content}"
        message_id = hashlib.sha256(url.encode()).hexdigest()
        return CollectedMessage(message_id=message_id, text=normalized, url=url), requests

    async def fetch(self, source: Source) -> FetchResult:
        """抓列表页，产出列表页自身内容（如果有资源链接）与详情页内容。

        先按 `_detail_pattern(source)`（或 `source.extra["detail_pattern"]`
        覆盖）与「正文内容链接」双重判定筛出候选详情页 URL，按「已在正文里
        出现的链接优先、URL 中最长数字串靠前」排序后，最多取
        `source.max_pages_per_fetch` 个未采过的详情页逐个访问；详情页里若带
        `.torrent` 附件会一并下载解出 magnet 链接。已处理过的消息 ID（对
        列表页自身内容、每个详情页分别算一个 ID）记入去重窗口
        `Source.extra[_SEEN_KEY]`（滚动保留最新 `_MAX_SEEN` 个）。

        Args:
            source: 待采集的源，`source.url` 为列表页地址。

        Returns:
            FetchResult：`messages` 为本轮新产出的消息；`truncated` 表示还有
            未采的候选详情页；`title` 取列表页标题或域名兜底；
            `backfill_done` 恒为 True，因为没有「补历史」语义。

        Raises:
            httpx.HTTPError: 列表页请求失败，或所有候选详情页都请求失败
                （只要有至少一个详情页被成功处理过，单个详情页的请求错误会
                被吞掉记作 404/失败跳过，不会向上抛出）。
        """
        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        pages = 1
        try:
            response = await client.get(source.url, headers={"User-Agent": DEFAULT_UA})
            response.raise_for_status()
            value = _decode(response.content)
            listing = _parse_page(value)
            detail_urls: dict[str, str] = {}
            detail_pattern = _detail_pattern(source)
            for href, label in sorted(
                listing.links,
                key=lambda item: (
                    item[0] not in listing.content_links,
                    -max((len(value) for value in re.findall(r"\d+", item[0])), default=0),
                ),
            ):
                url = urljoin(str(response.url), href)
                if _host(url) == _host(str(response.url)) and (
                    detail_pattern.search(url) or href in listing.content_links
                ):
                    clean, _fragment = urldefrag(url)
                    detail_urls.setdefault(clean, label)

            raw_seen = (source.extra or {}).get(_SEEN_KEY, [])
            if not isinstance(raw_seen, list):
                raw_seen = []
            seen = {str(value) for value in raw_seen if value}
            content = _page_content(value)
            direct_links = scan_known_links(content)
            messages: list[CollectedMessage] = []
            attempted: list[str] = []
            if direct_links:
                direct_id = hashlib.sha256(
                    "\n".join((str(response.url), *(link.url for link in direct_links))).encode()
                ).hexdigest()
                if direct_id not in seen:
                    normalized = collection_text(content) or content
                    messages.append(
                        CollectedMessage(
                            message_id=direct_id,
                            text=normalized,
                            url=str(response.url),
                        )
                    )
                    attempted.append(direct_id)
            pending = [
                (url, label)
                for url, label in detail_urls.items()
                if hashlib.sha256(url.encode()).hexdigest() not in seen
            ]
            selected = pending[: max(1, source.max_pages_per_fetch)]
            detail_attempted: list[str] = []
            errors: list[httpx.HTTPError] = []
            for index, (url, listing_title) in enumerate(selected, 1):
                try:
                    message, used = await self._detail_message(client, url, listing_title)
                except httpx.HTTPStatusError as exc:
                    pages += 1
                    if exc.response.status_code == 404:
                        message_id = hashlib.sha256(url.encode()).hexdigest()
                        attempted.append(message_id)
                        detail_attempted.append(message_id)
                    else:
                        errors.append(exc)
                    self._report("fetch", index, len(selected), len(messages), position=url)
                    continue
                except httpx.HTTPError as exc:
                    pages += 1
                    errors.append(exc)
                    self._report("fetch", index, len(selected), len(messages), position=url)
                    continue
                pages += used
                message_id = hashlib.sha256(url.encode()).hexdigest()
                attempted.append(message_id)
                detail_attempted.append(message_id)
                if message:
                    messages.append(message)
                self._report("fetch", index, len(selected), len(messages), position=url)
            if errors and not detail_attempted:
                raise errors[0]
        finally:
            if self._owns_client:
                await client.aclose()

        recent = list(dict.fromkeys([*(str(value) for value in raw_seen if value), *attempted]))
        return FetchResult(
            messages=messages,
            pages_fetched=pages,
            truncated=len(pending) > len(detail_attempted),
            title=listing.title or _host(source.url),
            state={_SEEN_KEY: recent[-_MAX_SEEN:]},
            backfill_done=True,
        )

    async def backfill(self, source: Source) -> FetchResult:
        """列表页采集没有「补历史」语义，直接返回已完成。"""
        return FetchResult(backfill_done=True)
