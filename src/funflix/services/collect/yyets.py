"""人人影视公开 API 采集器。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress

_HOSTS = {"yyets.click", "www.yyets.click"}
_SEEN_KEY = "yyets_seen_versions"
_MAX_SEEN = 1000
_LATEST_SIZE = 100
_DETAIL_CONCURRENCY = 8


def _base_url(url: str) -> str | None:
    parts = urlsplit(url.strip())
    if (
        parts.scheme.lower() not in {"http", "https"}
        or (parts.hostname or "").lower() not in _HOSTS
    ):
        return None
    return f"{parts.scheme.lower()}://{parts.netloc}"


def _latest_versions(payload: Any) -> list[tuple[int, int]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("人人影视 latest API 返回格式无效")
    versions: dict[int, int] = {}
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        try:
            resource_id = int(row["resource_id"])
            timestamp = int(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if resource_id > 0 and timestamp > versions.get(resource_id, 0):
            versions[resource_id] = timestamp
    return sorted(versions.items(), key=lambda item: (item[1], item[0]))


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def format_resource(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("人人影视 resource API 返回格式无效")
    data = payload["data"]
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    title = _one_line(info.get("cnname") or info.get("enname"))
    if not title:
        raise ValueError("人人影视资源缺少标题")

    lines = [f"名称：{title}"]
    for label, value in (
        ("英文名", info.get("enname")),
        ("别名", info.get("aliasname")),
        ("地区", info.get("area")),
        ("类型", info.get("channel_cn")),
        ("年份", "/".join(str(year) for year in info.get("year", []) if year)),
    ):
        if cleaned := _one_line(value):
            lines.append(f"{label}：{cleaned}")

    seasons = data.get("list") if isinstance(data.get("list"), list) else []
    for season in seasons:
        if not isinstance(season, dict):
            continue
        season_name = _one_line(season.get("season_cn"))
        formats = season.get("items") if isinstance(season.get("items"), dict) else {}
        for format_name, items in formats.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                description = " ".join(
                    part
                    for part in (
                        season_name,
                        _one_line(format_name),
                        _one_line(item.get("episode")),
                        _one_line(item.get("name")),
                        _one_line(item.get("size")),
                    )
                    if part
                )
                if description:
                    lines.append(description)
                files = item.get("files") if isinstance(item.get("files"), list) else []
                for file in files:
                    if not isinstance(file, dict) or not (
                        address := _one_line(file.get("address"))
                    ):
                        continue
                    way = _one_line(file.get("way_cn")) or "链接"
                    passcode = _one_line(file.get("passwd"))
                    suffix = f" 提取码：{passcode}" if passcode else ""
                    lines.append(f"{way}：{address}{suffix}")
    return "\n".join(lines)


class YYeTsCollector(SupportsProgress):
    name = "yyets-api-v1"
    detect_priority = 20

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        base = _base_url(url)
        if base is None or urlsplit(url).path.rstrip("/") not in {"", "/api/resource/latest"}:
            return None
        return "yyets.click"

    async def fetch(self, source: Source) -> FetchResult:
        base = _base_url(source.url)
        if base is None:
            raise ValueError(f"无效的人人影视 API 地址：{source.url}")
        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        headers = {"User-Agent": DEFAULT_UA}
        try:
            latest = await client.get(
                f"{base}/api/resource/latest", params={"size": _LATEST_SIZE}, headers=headers
            )
            latest.raise_for_status()
            versions = _latest_versions(latest.json())

            raw_seen = (source.extra or {}).get(_SEEN_KEY, [])
            if not isinstance(raw_seen, list):
                raw_seen = []
            raw_seen = [str(value) for value in raw_seen if value]
            seen = set(raw_seen)
            fresh = [
                (resource_id, timestamp)
                for resource_id, timestamp in versions
                if f"{resource_id}:{timestamp}" not in seen
            ]

            semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)

            async def load(resource_id: int, timestamp: int) -> CollectedMessage:
                async with semaphore:
                    response = await client.get(
                        f"{base}/api/resource", params={"id": resource_id}, headers=headers
                    )
                    response.raise_for_status()
                return CollectedMessage(
                    message_id=f"{resource_id}:{timestamp}",
                    text=format_resource(response.json()),
                    published_at=datetime.fromtimestamp(timestamp, UTC),
                    url=f"{base}/resource/{resource_id}",
                )

            messages = list(await asyncio.gather(*(load(*version) for version in fresh)))
        finally:
            if self._owns_client:
                await client.aclose()

        # ponytail: timestamp is the only public update cursor; refetch full details if the API
        # later exposes a revision or paginated change feed.
        ordered_ids = list(
            dict.fromkeys([*raw_seen, *(message.message_id for message in messages)])
        )
        state = {_SEEN_KEY: ordered_ids[-_MAX_SEEN:]}
        self._report(
            "fetch", 1, 1, len(messages), position=messages[-1].message_id if messages else None
        )
        return FetchResult(
            messages=messages,
            pages_fetched=1,
            title="人人影视",
            state=state,
            backfill_done=True,
        )

    async def backfill(self, source: Source) -> FetchResult:
        return FetchResult(backfill_done=True)
