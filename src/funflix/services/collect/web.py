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
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "a" and (href := dict(attrs).get("href")):
            self._href = href
            self._label = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._label).strip()))
            self._href = None
            self._label = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._href is not None:
            self._label.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())


def _parse_page(value: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(value)
    parser.close()
    parser.links.extend(
        (html.unescape(match.group("url")), "") for match in _ONCLICK_URL_RE.finditer(value)
    )
    return parser


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
    name = "public-web-v1"
    detect_priority = 950

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        return _canonical_url(url)

    async def _detail_message(
        self, client: httpx.AsyncClient, url: str, listing_title: str
    ) -> tuple[CollectedMessage | None, int]:
        response = await client.get(url, headers={"User-Agent": DEFAULT_UA})
        response.raise_for_status()
        value = _decode(response.content)
        page = _parse_page(value)
        text = _text(value)
        extra_magnets: list[str] = []
        requests = 1
        attachments = [
            (urljoin(url, href), label)
            for href, label in page.links
            if label.lower().endswith(".torrent") or "attach-download-" in href.lower()
        ]
        for attachment_url, label in attachments[:5]:
            torrent = await client.get(attachment_url, headers={"User-Agent": DEFAULT_UA})
            torrent.raise_for_status()
            requests += 1
            if magnet := _torrent_magnet(torrent.content, label or page.title):
                extra_magnets.append(magnet)

        content = "\n".join((text, *extra_magnets))
        links = scan_known_links(content)
        title = _title(html.unescape(listing_title or page.title))
        if not title or not links:
            return None, requests
        normalized = collection_text(content) or f"名称：{title}\n{content}"
        message_id = hashlib.sha256(url.encode()).hexdigest()
        return CollectedMessage(message_id=message_id, text=normalized, url=url), requests

    async def fetch(self, source: Source) -> FetchResult:
        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        pages = 1
        try:
            response = await client.get(source.url, headers={"User-Agent": DEFAULT_UA})
            response.raise_for_status()
            value = _decode(response.content)
            listing = _parse_page(value)
            detail_urls: dict[str, str] = {}
            detail_pattern = _detail_pattern(source)
            for href, label in listing.links:
                url = urljoin(str(response.url), href)
                if _host(url) == _host(str(response.url)) and detail_pattern.search(url):
                    clean, _fragment = urldefrag(url)
                    detail_urls.setdefault(clean, label)

            raw_seen = (source.extra or {}).get(_SEEN_KEY, [])
            if not isinstance(raw_seen, list):
                raw_seen = []
            seen = {str(value) for value in raw_seen if value}
            pending = [
                (url, label)
                for url, label in detail_urls.items()
                if hashlib.sha256(url.encode()).hexdigest() not in seen
            ]
            selected = pending[: max(1, source.max_pages_per_fetch)]
            messages: list[CollectedMessage] = []
            attempted: list[str] = []
            errors: list[httpx.HTTPError] = []
            for index, (url, listing_title) in enumerate(selected, 1):
                try:
                    message, used = await self._detail_message(client, url, listing_title)
                except httpx.HTTPStatusError as exc:
                    pages += 1
                    if exc.response.status_code == 404:
                        attempted.append(hashlib.sha256(url.encode()).hexdigest())
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
                attempted.append(hashlib.sha256(url.encode()).hexdigest())
                if message:
                    messages.append(message)
                self._report("fetch", index, len(selected), len(messages), position=url)
            if errors and not attempted:
                raise errors[0]
        finally:
            if self._owns_client:
                await client.aclose()

        recent = list(dict.fromkeys([*(str(value) for value in raw_seen if value), *attempted]))
        return FetchResult(
            messages=messages,
            pages_fetched=pages,
            truncated=len(pending) > len(attempted),
            title=listing.title or _host(source.url),
            state={_SEEN_KEY: recent[-_MAX_SEEN:]},
            backfill_done=True,
        )

    async def backfill(self, source: Source) -> FetchResult:
        return FetchResult(backfill_done=True)
