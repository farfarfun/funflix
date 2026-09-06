"""电影云集（bbs.dyyjv.com）合集采集器。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress
from funflix.services.text.linkscan import scan_known_links

_HOST = "bbs.dyyjv.com"
_READER_API = f"https://r.jina.ai/https://{_HOST}/api/discussions/"
_HEAD_KEY = "dyyjv_head_id"
_DISCUSSION_RE = re.compile(rf"https?://{re.escape(_HOST)}/d/(\d+)", re.I)
_NUMBERED_TITLE_RE = re.compile(r"^\d{1,4}\s*[.、:：)）-]\s*(?P<title>\S.+)$")
_BOOK_TITLE_RE = re.compile(r"^《[^》]{1,200}》.*$")


class DYYJVError(RuntimeError):
    """站点返回了无法安全继续采集的数据。"""


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
        raise DYYJVError("电影云集接口未返回 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise DYYJVError("电影云集接口返回格式无效")
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


def _message(payload: dict[str, Any]) -> CollectedMessage | None:
    discussion_id, title, published_at, text = _discussion(payload)
    links = scan_known_links(text)
    if not discussion_id or not links:
        return None

    titles: list[str] = []
    for line in text.splitlines():
        match = _NUMBERED_TITLE_RE.match(line)
        candidate = (
            match.group("title") if match else line if _BOOK_TITLE_RE.fullmatch(line) else ""
        )
        if candidate and candidate not in titles:
            titles.append(candidate)
    if not titles:
        return None

    lines: list[str] = []
    for item in titles:
        lines.extend((f"名称：{item}", "类型：短剧"))
    lines.append("合集资源：")
    for link in links:
        suffix = f" 提取码：{link.passcode}" if link.passcode else ""
        lines.append(f"{link.provider.value}：{link.url}{suffix}")
    return CollectedMessage(
        message_id=discussion_id,
        text="\n".join(lines),
        published_at=published_at,
        url=f"https://{_HOST}/d/{discussion_id}",
    )


class DYYJVCollector(SupportsProgress):
    name = "dyyjv-forum-v1"
    detect_priority = 25

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._ids: list[int] = []

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        parts = urlsplit(url.strip())
        match = re.fullmatch(r"/d/(\d+)/?", parts.path)
        if parts.scheme.lower() in {"http", "https"} and parts.hostname == _HOST and match:
            return match.group(1)
        return None

    async def _get(self, client: httpx.AsyncClient, discussion_id: int | str) -> dict[str, Any]:
        response = await client.get(f"{_READER_API}{discussion_id}")
        response.raise_for_status()
        return _reader_json(response.text)

    async def _load_messages(
        self, client: httpx.AsyncClient, ids: list[int], stage: str
    ) -> list[CollectedMessage]:
        messages: list[CollectedMessage] = []
        for page, discussion_id in enumerate(ids, 1):
            if message := _message(await self._get(client, discussion_id)):
                messages.append(message)
            self._report(stage, page, len(ids), len(messages), position=discussion_id)
        return messages

    async def fetch(self, source: Source) -> FetchResult:
        client = self._client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        try:
            index = await self._get(client, source.identifier)
            self._ids = _index_ids(index, source.identifier)
            _id, title, _published, _text = _discussion(index)
            budget = max(1, source.max_pages_per_fetch)
            raw_head = source.extra.get(_HEAD_KEY)
            head = int(raw_head) if str(raw_head).isdigit() else None
            pending = [value for value in self._ids if head is not None and value > head]
            selected = pending[:budget] if head is not None else self._ids[-budget:]
            messages = await self._load_messages(client, selected, "fetch")
        finally:
            if self._owns_client:
                await client.aclose()

        state = {_HEAD_KEY: max(selected)} if selected else {}
        return FetchResult(
            messages=messages,
            pages_fetched=1 + len(selected),
            truncated=head is not None and len(pending) > len(selected),
            title=title or "电影云集",
            state=state,
        )

    async def backfill(self, source: Source) -> FetchResult:
        client = self._client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        loaded_index = False
        try:
            if not self._ids:
                self._ids = _index_ids(
                    await self._get(client, source.identifier), source.identifier
                )
                loaded_index = True
            raw_cursor = source.backfill_cursor_id
            cursor = (
                int(raw_cursor)
                if raw_cursor and raw_cursor.isdigit()
                else max(self._ids, default=0) + 1
            )
            pending = [value for value in self._ids if value < cursor]
            selected = pending[-max(1, source.max_pages_per_fetch) :]
            messages = await self._load_messages(client, selected, "backfill")
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
