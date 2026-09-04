"""人人影视历史快照与网友评论采集器。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from zipfile import ZipFile

import httpx

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress
from funflix.services.text.linkscan import scan_links

_SNAPSHOT_URL = "https://raw.githubusercontent.com/Greyh4t/yyets_database/master/yyets_sqlite.zip"
_SNAPSHOT_PATH = "/Greyh4t/yyets_database/master/yyets_sqlite.zip"
_SNAPSHOT_SHA1 = "ca63e91b80be58c0d4a419f65052eabb560d6384"
_SNAPSHOT_MEMBER = "yyets.sqlite"
_SNAPSHOT_STATE_KEY = "yyets_snapshot_sha1"
_COMMENT_PATH = "/api/comment/newest"
_COMMENT_RECENT_KEY = "yyets_comment_recent_ids"
_COMMENT_BACKFILL_PAGE_KEY = "yyets_comment_backfill_page"
_COMMENT_PAGE_SIZE = 100
_MAX_RECENT_COMMENTS = 1000


def _source_kind(url: str) -> str | None:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if parts.scheme.lower() in {"http", "https"}:
        if host == "raw.githubusercontent.com" and path == _SNAPSHOT_PATH:
            return "snapshot"
        if host in {"yyets.click", "www.yyets.click"} and path == _COMMENT_PATH:
            return "comments"
    return None


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def format_resource(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("人人影视资源数据格式无效")
    data = payload["data"]
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    title = _one_line(info.get("cnname") or info.get("enname"))
    if not title:
        raise ValueError("人人影视资源缺少标题")

    year = next((str(value) for value in info.get("year", []) if value), "")
    media_type = _one_line(info.get("channel_cn"))
    lines: list[str] = []
    seasons = data.get("list") if isinstance(data.get("list"), list) else []
    for season in seasons:
        if not isinstance(season, dict):
            continue
        season_name = _one_line(season.get("season_cn"))
        season_num = _one_line(season.get("season_num"))
        season_token = (
            f"S{int(season_num):02d}" if season_num.isdigit() and 0 < int(season_num) < 100 else ""
        )
        formats = season.get("items") if isinstance(season.get("items"), dict) else {}
        for format_name, items in formats.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                files = item.get("files") if isinstance(item.get("files"), list) else []
                valid_files = [
                    file
                    for file in files
                    if isinstance(file, dict) and _one_line(file.get("address"))
                ]
                if not valid_files:
                    continue

                anchor = " ".join(part for part in (title, season_token, year) if part)
                lines.append(f"名称：{anchor}")
                if media_type:
                    lines.append(f"类型：{media_type}")
                episode = _one_line(item.get("episode"))
                episode_label = (
                    f"第{episode}集" if episode.isdigit() and episode != "0" else episode
                )
                episode_info = " ".join(part for part in (season_name, episode_label) if part)
                if episode_info:
                    lines.append(f"季集：{episode_info}")
                if format_name:
                    lines.append(f"格式：{_one_line(format_name)}")
                if name := _one_line(item.get("name")):
                    lines.append(f"文件：{name}")
                if size := _one_line(item.get("size")):
                    lines.append(f"大小：{size}")
                for file in valid_files:
                    address = _one_line(file.get("address"))
                    way = _one_line(file.get("way_cn")) or "链接"
                    passcode = _one_line(file.get("passwd"))
                    suffix = f" 提取码：{passcode}" if passcode else ""
                    lines.append(f"{way}：{address}{suffix}")
    return "\n".join(lines) if lines else f"名称：{title} {year}".strip()


def _snapshot_messages(payload: bytes) -> list[CollectedMessage]:
    digest = hashlib.sha1(payload).hexdigest()  # noqa: S324 - published integrity checksum
    if digest != _SNAPSHOT_SHA1:
        raise ValueError(f"人人影视快照 SHA1 不匹配：{digest}")

    messages: list[CollectedMessage] = []
    with ZipFile(io.BytesIO(payload)) as archive:
        with (
            archive.open(_SNAPSHOT_MEMBER) as source,
            tempfile.NamedTemporaryFile(suffix=".sqlite") as target,
        ):
            shutil.copyfileobj(source, target)
            target.flush()
            connection = sqlite3.connect(f"file:{target.name}?mode=ro", uri=True)
            try:
                rows = connection.execute("SELECT id, data FROM yyets ORDER BY id")
                for resource_id, raw in rows:
                    text = format_resource(json.loads(raw))
                    if not scan_links(text):
                        continue
                    messages.append(
                        CollectedMessage(
                            message_id=str(resource_id),
                            text=text,
                            url=f"https://yyets.click/resource?id={resource_id}",
                        )
                    )
            finally:
                connection.close()
    return messages


def _comment_date(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _comment_messages(payload: Any, base_url: str) -> tuple[list[CollectedMessage], list[str], int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("人人影视评论 API 返回格式无效")
    try:
        total = int(payload.get("count", 0))
    except (TypeError, ValueError):
        total = 0

    messages: list[CollectedMessage] = []
    ids: list[str] = []
    for row in payload["data"]:
        if not isinstance(row, dict) or not (comment_id := _one_line(row.get("id"))):
            continue
        message_id = f"comment:{comment_id}"
        ids.append(message_id)
        content = str(row.get("content") or "").strip()
        if not content or not scan_links(content):
            continue

        try:
            resource_id = int(row.get("resource_id", 233))
        except (TypeError, ValueError):
            resource_id = 233
        title = _one_line(row.get("cnname"))
        text = f"名称：{title}\n{content}" if resource_id != 233 and title else content
        url = (
            f"{base_url}/discuss#{comment_id}"
            if resource_id == 233
            else f"{base_url}/resource?id={resource_id}#{comment_id}"
        )
        messages.append(
            CollectedMessage(
                message_id=message_id,
                text=text,
                published_at=_comment_date(row.get("date")),
                url=url,
            )
        )
    return messages, ids, total


class YYeTsCollector(SupportsProgress):
    name = "yyets-v1"
    detect_priority = 30

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        kind = _source_kind(url)
        if kind == "snapshot":
            return "yyets-snapshot-2021-08-22"
        if kind == "comments":
            return "yyets-comments"
        return None

    async def _get(self, url: str, **params: int) -> httpx.Response:
        client = self._client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        try:
            response = await client.get(url, params=params, headers={"User-Agent": DEFAULT_UA})
            response.raise_for_status()
            return response
        finally:
            if self._owns_client:
                await client.aclose()

    async def fetch(self, source: Source) -> FetchResult:
        kind = _source_kind(source.url)
        if kind == "snapshot":
            if (source.extra or {}).get(_SNAPSHOT_STATE_KEY) == _SNAPSHOT_SHA1:
                return FetchResult(title="人人影视历史快照", backfill_done=True)
            response = await self._get(source.url)
            messages = _snapshot_messages(response.content)
            self._report("fetch", 1, 1, len(messages), position=_SNAPSHOT_SHA1[:8])
            return FetchResult(
                messages=messages,
                pages_fetched=1,
                title="人人影视历史快照",
                state={_SNAPSHOT_STATE_KEY: _SNAPSHOT_SHA1},
                backfill_done=True,
            )
        if kind != "comments":
            raise ValueError(f"无效的人人影视采集地址：{source.url}")

        response = await self._get(source.url, page=1, size=_COMMENT_PAGE_SIZE)
        parts = urlsplit(source.url)
        base_url = f"{parts.scheme}://{parts.netloc}"
        messages, ids, _total = _comment_messages(response.json(), base_url)
        raw_seen = (source.extra or {}).get(_COMMENT_RECENT_KEY, [])
        if not isinstance(raw_seen, list):
            raw_seen = []
        raw_seen = [str(value) for value in raw_seen if value]
        seen = set(raw_seen)
        fresh = [message for message in messages if message.message_id not in seen]
        recent = list(dict.fromkeys([*raw_seen, *ids]))[-_MAX_RECENT_COMMENTS:]
        self._report("fetch", 1, 1, len(fresh), position=ids[0] if ids else None)
        return FetchResult(
            messages=fresh,
            pages_fetched=1,
            title="人人影视网友评论",
            state={_COMMENT_RECENT_KEY: recent},
        )

    async def backfill(self, source: Source) -> FetchResult:
        if _source_kind(source.url) != "comments":
            return FetchResult(backfill_done=True)
        try:
            page = max(2, int((source.extra or {}).get(_COMMENT_BACKFILL_PAGE_KEY, 2)))
        except (TypeError, ValueError):
            page = 2
        response = await self._get(source.url, page=page, size=_COMMENT_PAGE_SIZE)
        parts = urlsplit(source.url)
        base_url = f"{parts.scheme}://{parts.netloc}"
        messages, ids, total = _comment_messages(response.json(), base_url)
        done = not ids or page * _COMMENT_PAGE_SIZE >= total
        self._report("backfill", page, max(1, (total + 99) // 100), len(messages), page)
        return FetchResult(
            messages=messages,
            pages_fetched=1 if ids else 0,
            state={_COMMENT_BACKFILL_PAGE_KEY: page + 1},
            backfill_cursor=str(page),
            backfill_done=done,
        )
