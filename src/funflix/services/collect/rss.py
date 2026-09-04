"""通用 RSS/Atom 采集器。

RSS 是最便宜的扩展面：站点只要提供公开 feed，就能接入而不用为每个站点
再写一套翻页逻辑。条目 ID 保存在 ``Source.extra``，因此兼容没有数字水位的
feed，也不会误用 ``cursor_message_id`` 的整数语义。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urldefrag, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress

_SEEN_KEY = "rss_seen_ids"
# ponytail: keep 2000 IDs in JSON; content_hash still catches older repeats, use a
# dedicated cursor table only if feeds need exact change tracking beyond this window.
_MAX_SEEN = 2000
_HEX_HASH_RE = re.compile(r"^[0-9a-f]{20,64}$", re.I)


class _HTMLTextParser(HTMLParser):
    """把 feed 描述里的 HTML 转成文本，同时保留真实链接。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.lower().startswith(("http://", "https://", "magnet:")):
                self.parts.extend((" ", href, " "))

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _text(value: str | None) -> str:
    if not value:
        return ""
    parser = _HTMLTextParser()
    try:
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    except Exception:
        # HTML in a feed is untrusted decoration; the XML item itself remains usable.
        pass
    lines = [line.strip() for line in value.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _children(node: ET.Element, names: set[str]) -> list[ET.Element]:
    return [child for child in node if _local_name(child.tag) in names]


def _first_text(node: ET.Element, names: set[str]) -> str:
    for child in _children(node, names):
        value = "".join(child.itertext()).strip()
        if value:
            return value
    return ""


def _entry_links(node: ET.Element) -> list[str]:
    links: list[str] = []
    for child in _children(node, {"link", "enclosure", "content", "magneturi"}):
        href = (
            child.attrib.get("href")
            or child.attrib.get("url")
            or (child.text or "").strip()
        )
        if href and href.lower().startswith(("http://", "https://", "magnet:")):
            links.append(href)
    return list(dict.fromkeys(links))


def _published(value: str) -> datetime | None:
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _stable_id(node: ET.Element, title: str, body: str, links: list[str]) -> str:
    value = _first_text(node, {"guid", "id"}) or (links[0] if links else "")
    value = value.strip()
    if not value:
        value = "\n".join((title, body, *links))
    if len(value) <= 128:
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _item_message(node: ET.Element) -> CollectedMessage | None:
    title = _text(_first_text(node, {"title"}))
    body = _text(_first_text(node, {"description", "summary", "encoded", "content"}))
    links = _entry_links(node)

    # Nyaa and some other torrent feeds expose an infoHash as an extension field.
    # Turn it into a standard magnet so the existing link scanner can index it.
    info_hash = _first_text(node, {"infohash"}).strip()
    if info_hash and _HEX_HASH_RE.fullmatch(info_hash):
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}"
        if magnet not in links:
            links.append(magnet)

    parts = [part for part in (title, body) if part]
    parts.extend(link for link in links if link not in "\n".join(parts))
    text = "\n".join(parts).strip()
    if not text:
        return None

    return CollectedMessage(
        message_id=_stable_id(node, title, body, links),
        text=text,
        published_at=_published(_first_text(node, {"pubdate", "published", "updated", "date"})),
        url=links[0] if links else None,
    )


def parse_feed(payload: bytes | str) -> tuple[list[CollectedMessage], str | None]:
    """解析 RSS 2.0 或 Atom，返回按发布时间/原顺序排列的条目。"""
    root = ET.fromstring(payload)
    title = _first_text(root, {"title"})
    if not title:
        channels = _children(root, {"channel"})
        title = _first_text(channels[0], {"title"}) if channels else ""
    title = title or None
    nodes = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    messages = [message for node in nodes if (message := _item_message(node)) is not None]
    if any(message.published_at is not None for message in messages):
        # Stable sort keeps feeds without dates in their original order.
        messages.sort(key=lambda message: message.published_at or datetime.max.replace(tzinfo=UTC))
    return messages, title


def _canonical_url(url: str) -> str | None:
    candidate = url.strip()
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    clean, _fragment = urldefrag(urlunsplit(parts))
    return clean


class RSSCollector(SupportsProgress):
    name = "rss-atom-v1"
    detect_priority = 100

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        candidate = _canonical_url(url)
        if candidate is None:
            return None
        parts = urlsplit(candidate)
        path = parts.path.lower()
        query = parts.query.lower()
        looks_like_feed = (
            path.endswith((".xml", ".rss", ".atom"))
            or "/rss" in path
            or "/feed" in path
            or "rss" in path.rsplit("/", 1)[-1]
            or re.search(r"(?:^|&)page=rss(?:&|$)", query) is not None
            or re.search(r"(?:^|&)(?:format|output)=atom(?:&|$)", query) is not None
        )
        return candidate if looks_like_feed else None

    async def fetch(self, source: Source) -> FetchResult:
        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        try:
            response = await client.get(source.url, headers={"User-Agent": DEFAULT_UA})
            response.raise_for_status()
            messages, title = parse_feed(response.content)
        finally:
            if self._owns_client:
                await client.aclose()

        raw_seen = (source.extra or {}).get(_SEEN_KEY, [])
        if not isinstance(raw_seen, list):
            raw_seen = []
        raw_seen = [str(value) for value in raw_seen if value]
        seen = {str(value) for value in raw_seen if value}
        fresh = [message for message in messages if message.message_id not in seen]
        ordered_ids = list(
            dict.fromkeys([*raw_seen, *(message.message_id for message in messages)])
        )
        state = {_SEEN_KEY: ordered_ids[-_MAX_SEEN:]}
        self._report(
            "fetch", 1, 1, len(fresh), position=messages[-1].message_id if messages else None
        )
        return FetchResult(
            messages=fresh,
            pages_fetched=1,
            title=title,
            state=state,
            backfill_done=True,
        )

    async def backfill(self, source: Source) -> FetchResult:
        return FetchResult(backfill_done=True)
