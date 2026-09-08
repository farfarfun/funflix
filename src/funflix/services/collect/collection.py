"""集合页采集器：从索引页追踪多个资源详情页。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress
from funflix.services.text.linkscan import scan_known_links

_READER_PREFIX = "https://r.jina.ai/"
_HEAD_KEY = "forum_head_id"
_DISCUSSION_RE = re.compile(r"https?://[^/\s<>\"']+/d/(\d+)", re.I)
_NUMBERED_TITLE_RE = re.compile(r"^\d{1,4}\s*[.、:：)）-]\s*(?P<title>\S.+)$")
_NAMED_TITLE_RE = re.compile(r"^(?:名称|片名|剧名|标题|资源名称|影片名)\s*[:：]\s*(?P<title>\S.+)$")
_BOOK_TITLE_RE = re.compile(r"^(?P<title>《[^》]{1,200}》)")
_TITLE_HEADERS = {
    "name",
    "title",
    "名称",
    "片名",
    "剧名",
    "标题",
    "资源名称",
    "影片名",
    "电影名",
    "剧集名",
    "番名",
}
_URL_SCHEME_RE = re.compile(r"(?:https?://|magnet:|ed2k://)", re.I)
_URL_SUFFIX_RE = re.compile(r"\s+(?:https?://|magnet:|ed2k://).*$", re.I)


class CollectionError(RuntimeError):
    """集合页返回了无法安全继续采集的数据。"""


class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "hr"}:
            self.parts.append("\n")
        if tag == "a" and (href := dict(attrs).get("href")):
            self.parts.extend((" ", href, " "))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_text(value: str) -> str:
    parser = _HTMLTextParser()
    parser.feed(value)
    parser.close()
    return "\n".join(
        line for raw in "".join(parser.parts).splitlines() if (line := " ".join(raw.split()))
    )


def _reader_json(text: str) -> dict[str, Any]:
    marker = "Markdown Content:\n"
    raw = text.split(marker, 1)[1].strip() if marker in text else text.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectionError("集合页接口未返回 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise CollectionError("集合页接口返回格式无效")
    return payload


def _discussion(payload: dict[str, Any]) -> tuple[str, str, datetime | None, str]:
    data = payload["data"]
    attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    title = str(attributes.get("title") or "").strip()
    html = "\n".join(
        str(item["attributes"]["contentHtml"])
        for item in payload.get("included", [])
        if isinstance(item, dict)
        and item.get("type") == "posts"
        and isinstance(item.get("attributes"), dict)
        and item["attributes"].get("contentHtml")
    )
    published_at = None
    if created_at := attributes.get("createdAt"):
        try:
            published_at = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            published_at = (
                published_at.replace(tzinfo=UTC)
                if published_at.tzinfo is None
                else published_at.astimezone(UTC)
            )
        except ValueError:
            pass
    return str(data.get("id") or ""), title, published_at, _html_text(html)


def _index_ids(payload: dict[str, Any], index_id: str) -> list[int]:
    _id, _title, _published, text = _discussion(payload)
    return sorted({int(value) for value in _DISCUSSION_RE.findall(text) if value != index_id})


def collection_text(text: str) -> str | None:
    """规范多名称详情页；逐项地址保留原布局，共享地址显式标记。"""
    links = scan_known_links(text)
    if not links:
        return None

    titles: list[str] = []
    title_span: list[tuple[int, int]] = []
    table_title_column: int | None = None
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.strip()
        match = _NUMBERED_TITLE_RE.match(line) or _NAMED_TITLE_RE.match(line)
        book = _BOOK_TITLE_RE.match(line)
        cells = (
            [cell.strip() for cell in line[line.index("|") + 1 :].strip("|").split("|")]
            if line.count("|") >= 2
            else []
        )
        if cells and not _URL_SCHEME_RE.search(line):
            table_title_column = next(
                (index for index, cell in enumerate(cells) if cell.casefold() in _TITLE_HEADERS),
                table_title_column,
            )
        elif line and not cells:
            table_title_column = None
        table_title = (
            cells[table_title_column if table_title_column is not None else 0]
            if cells and _URL_SCHEME_RE.search(line)
            else ""
        )
        candidate = (
            match.group("title")
            if match
            else book.group("title")
            if book
            else table_title
            if table_title
            else ""
        )
        candidate = _URL_SUFFIX_RE.sub("", candidate).strip()
        if candidate and candidate not in titles:
            titles.append(candidate)
            title_span.append((offset, offset + len(raw_line)))
        offset += len(raw_line)
    if len(titles) < 2:
        return None

    first_title, last_title = title_span[0][0], title_span[-1][1]
    if any(first_title <= link.start < last_title for link in links):
        lines: list[str] = []
        attributed = 0
        for index, (title, (start, _end)) in enumerate(zip(titles, title_span, strict=True)):
            end = title_span[index + 1][0] if index + 1 < len(title_span) else last_title
            own_links = [link for link in links if start <= link.start < end]
            if own_links:
                attributed += 1
            lines.append(f"名称：{title}")
            for link in own_links:
                suffix = f" 提取码：{link.passcode}" if link.passcode else ""
                lines.append(f"{link.provider.value}：{link.url}{suffix}")
        if attributed >= 2:
            lines.extend(("原文：", text))
            return "\n".join(lines)
        return text

    lines: list[str] = []
    for item in titles:
        lines.append(f"名称：{item}")
    lines.append("合集资源：")
    for link in links:
        suffix = f" 提取码：{link.passcode}" if link.passcode else ""
        lines.append(f"{link.provider.value}：{link.url}{suffix}")
    return "\n".join(lines)


def _message(payload: dict[str, Any], origin: str) -> CollectedMessage | None:
    discussion_id, _title, published_at, text = _discussion(payload)
    normalized = collection_text(text)
    if not discussion_id or not normalized:
        return None
    return CollectedMessage(
        message_id=discussion_id,
        text=normalized,
        published_at=published_at,
        url=f"{origin}/d/{discussion_id}",
    )


def _target(url: str) -> tuple[str, str]:
    parts = urlsplit(url.strip())
    match = re.fullmatch(r"/d/(\d+)/?", parts.path)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or not match:
        raise CollectionError("无效的集合页地址")
    return urlunsplit((parts.scheme.lower(), parts.netloc, "", "", "")), match.group(1)


class CollectionCollector(SupportsProgress):
    name = "collection-page-v1"
    detect_priority = 25

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._ids: list[int] = []

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        parts = urlsplit(url.strip())
        match = re.fullmatch(r"/d/(\d+)/?", parts.path)
        if parts.scheme.lower() in {"http", "https"} and parts.hostname and match:
            identifier = f"{parts.hostname.lower()}:{match.group(1)}"
            return (
                identifier
                if len(identifier) <= 128
                else "sha256:" + hashlib.sha256(identifier.encode()).hexdigest()
            )
        return None

    async def _get(
        self, client: httpx.AsyncClient, api: str, discussion_id: int | str
    ) -> dict[str, Any]:
        response = await client.get(f"{api}{discussion_id}")
        response.raise_for_status()
        return _reader_json(response.text)

    async def _load_messages(
        self, client: httpx.AsyncClient, api: str, origin: str, ids: list[int], stage: str
    ) -> list[CollectedMessage]:
        messages: list[CollectedMessage] = []
        for page, discussion_id in enumerate(ids, 1):
            if message := _message(await self._get(client, api, discussion_id), origin):
                messages.append(message)
            self._report(stage, page, len(ids), len(messages), position=discussion_id)
        return messages

    async def fetch(self, source: Source) -> FetchResult:
        origin, index_id = _target(source.url)
        api = f"{_READER_PREFIX}{origin}/api/discussions/"
        client = self._client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        try:
            index = await self._get(client, api, index_id)
            self._ids = _index_ids(index, index_id)
            _id, title, _published, _text = _discussion(index)
            budget = max(1, source.max_pages_per_fetch)
            raw_head = source.extra.get(_HEAD_KEY)
            head = int(raw_head) if str(raw_head).isdigit() else None
            pending = [value for value in self._ids if head is not None and value > head]
            selected = pending[:budget] if head is not None else self._ids[-budget:]
            messages = await self._load_messages(client, api, origin, selected, "fetch")
        finally:
            if self._owns_client:
                await client.aclose()

        state = {_HEAD_KEY: max(selected)} if selected else {}
        return FetchResult(
            messages=messages,
            pages_fetched=1 + len(selected),
            truncated=head is not None and len(pending) > len(selected),
            title=title or "资源合集",
            state=state,
        )

    async def backfill(self, source: Source) -> FetchResult:
        origin, index_id = _target(source.url)
        api = f"{_READER_PREFIX}{origin}/api/discussions/"
        client = self._client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        loaded_index = False
        try:
            if not self._ids:
                self._ids = _index_ids(await self._get(client, api, index_id), index_id)
                loaded_index = True
            raw_cursor = source.backfill_cursor_id
            cursor = (
                int(raw_cursor)
                if raw_cursor and raw_cursor.isdigit()
                else max(self._ids, default=0) + 1
            )
            pending = [value for value in self._ids if value < cursor]
            selected = pending[-max(1, source.max_pages_per_fetch) :]
            messages = await self._load_messages(client, api, origin, selected, "backfill")
        finally:
            if self._owns_client:
                await client.aclose()

        new_cursor = min(selected) if selected else cursor
        return FetchResult(
            messages=messages,
            pages_fetched=int(loaded_index) + len(selected),
            backfill_cursor=str(new_cursor),
            backfill_done=not any(value < new_cursor for value in self._ids),
        )
