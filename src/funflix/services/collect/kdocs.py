"""金山文档多维表格采集器。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress

logger = logging.getLogger(__name__)

_LINK_RE = re.compile(r"^https?://(?:www\.)?kdocs\.cn/l/(?P<id>[A-Za-z0-9_-]+)", re.I)
_OFFSETS_KEY = "kdocs_offsets"
_TAILS_KEY = "kdocs_tail_offsets"
_TOTALS_KEY = "kdocs_totals"
_DONE_KEY = "kdocs_completed_sheets"


class KDocsError(RuntimeError):
    """公开接口返回了无法继续采集的响应。"""


def cell_to_text(value: Any) -> str:
    """把多维表格单元格压成抽取器可读的文本。"""
    parts: list[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            address = node.get("address")
            if isinstance(address, str) and address.strip():
                parts.append(address.strip())
            else:
                for item in node.values():
                    collect(item)
        elif isinstance(node, list):
            for item in node:
                collect(item)
        elif isinstance(node, str) and node.strip():
            parts.append(node.strip())
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            parts.append(str(node))

    collect(value)
    return " ".join(dict.fromkeys(parts))


def render_record(fields: dict[str, Any], ignored_fields: set[str] | None = None) -> str:
    lines: list[str] = []
    for label, cell in fields.items():
        if ignored_fields and label in ignored_fields:
            continue
        value = cell_to_text(cell)
        if value:
            lines.append(f"{label}：{value}")
    return "\n".join(lines)


class KDocsCollector(SupportsProgress):
    name = "kdocs-database-v1"
    detect_priority = 15

    def __init__(self, client: httpx.AsyncClient | None = None, page_delay: float = 0.1) -> None:
        self._client = client
        self._owns_client = client is None
        self._page_delay = page_delay
        self._sheets: dict[str, dict[str, Any]] = {}

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        match = _LINK_RE.match(url.strip())
        return match.group("id") if match else None

    @staticmethod
    def _headers(source: Source) -> dict[str, str]:
        query = urlsplit(source.url).query
        headers = {
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": "https://www.kdocs.cn",
            "Referer": source.url.replace("http://", "https://", 1),
            "User-Agent": DEFAULT_UA,
        }
        if query:
            headers["X-User-Query"] = query
        return headers

    async def _execute(
        self, client: httpx.AsyncClient, source: Source, command: str, param: dict[str, Any]
    ) -> dict[str, Any]:
        response = await client.post(
            f"https://www.kdocs.cn/api/v3/office/file/{source.identifier}/core/execute",
            headers=self._headers(source),
            json={"command": command, "param": param},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("result") != "ok" or not isinstance(payload.get("detail"), dict):
            raise KDocsError(f"KDocs 接口失败：{payload.get('error') or payload.get('result')}")
        return payload["detail"]

    async def _list_sheets(
        self, client: httpx.AsyncClient, source: Source
    ) -> list[dict[str, Any]]:
        detail = await self._execute(
            client, source, "http.db.listSheets", {"showVeryhidden": False}
        )
        sheets = detail.get("sheets")
        if not isinstance(sheets, list):
            raise KDocsError("KDocs 响应中没有 sheet 清单")
        return [sheet for sheet in sheets if isinstance(sheet, dict)]

    async def _list_records(
        self, client: httpx.AsyncClient, source: Source, sheet_id: int, offset: str | None
    ) -> dict[str, Any]:
        param: dict[str, Any] = {
            "sheetId": sheet_id,
            "preferId": False,
            "showRecordExtraInfo": False,
            "showFieldsInfo": False,
        }
        if offset:
            param["offset"] = offset
        return await self._execute(client, source, "http.db.listRecords", param)

    @staticmethod
    def _ignored_fields(sheet: dict[str, Any]) -> set[str]:
        return {
            str(field["name"])
            for field in sheet.get("fields") or []
            if isinstance(field, dict) and field.get("type") == "Attachment" and field.get("name")
        }

    @staticmethod
    def _messages(
        source: Source, sheet: dict[str, Any], detail: dict[str, Any]
    ) -> list[CollectedMessage]:
        sheet_id = str(sheet["id"])
        ignored = KDocsCollector._ignored_fields(sheet)
        now = datetime.now(UTC)
        messages: list[CollectedMessage] = []
        for record in detail.get("records") or []:
            if not isinstance(record, dict) or not isinstance(record.get("fields"), dict):
                continue
            text = render_record(record["fields"], ignored)
            if not text:
                continue
            messages.append(
                CollectedMessage(
                    message_id=f"{sheet_id}:{record.get('id')}",
                    text=text,
                    published_at=now,
                    url=f"https://www.kdocs.cn/l/{source.identifier}",
                )
            )
        return messages

    @staticmethod
    def _state(
        offsets: dict[str, str], tails: dict[str, str], totals: dict[str, int], done: set[str]
    ) -> dict[str, Any]:
        return {
            _OFFSETS_KEY: offsets,
            _TAILS_KEY: tails,
            _TOTALS_KEY: totals,
            _DONE_KEY: sorted(done),
        }

    async def fetch(self, source: Source) -> FetchResult:
        """枚举表格；首次取首页，已完成的表只在行数增长后续扫。"""
        offsets: dict[str, str] = dict(source.extra.get(_OFFSETS_KEY) or {})
        tails: dict[str, str] = dict(source.extra.get(_TAILS_KEY) or {})
        old_totals: dict[str, int] = dict(source.extra.get(_TOTALS_KEY) or {})
        totals = dict(old_totals)
        done = set(source.extra.get(_DONE_KEY) or [])
        messages: list[CollectedMessage] = []
        pages = 0
        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)

        try:
            sheets = await self._list_sheets(client, source)
            self._sheets = {str(s.get("id")): s for s in sheets}
            pages += 1
            for sheet in sheets:
                if not isinstance(sheet.get("id"), int):
                    continue
                sheet_id = str(sheet["id"])
                total = sheet.get("recordsCount")
                if isinstance(total, int) and total >= 0:
                    totals[sheet_id] = total

                if sheet_id in offsets:
                    continue
                if sheet_id in done and totals.get(sheet_id, 0) <= old_totals.get(sheet_id, 0):
                    continue

                start = tails.get(sheet_id) if sheet_id in done else None
                detail = await self._list_records(client, source, sheet["id"], start)
                pages += 1
                messages.extend(self._messages(source, sheet, detail))
                next_offset = detail.get("offset")
                if start:
                    tails[sheet_id] = start
                if isinstance(next_offset, str) and next_offset:
                    offsets[sheet_id] = next_offset
                    done.discard(sheet_id)
                else:
                    offsets.pop(sheet_id, None)
                    done.add(sheet_id)
        finally:
            if self._owns_client:
                await client.aclose()

        pending = bool(offsets)
        title = " / ".join(str(s.get("name")) for s in sheets if s.get("name")) or None
        return FetchResult(
            messages=messages,
            pages_fetched=pages,
            truncated=pending,
            title=title,
            state=self._state(offsets, tails, totals, done),
            backfill_pending=pending,
        )

    async def backfill(self, source: Source) -> FetchResult:
        offsets: dict[str, str] = dict(source.extra.get(_OFFSETS_KEY) or {})
        tails: dict[str, str] = dict(source.extra.get(_TAILS_KEY) or {})
        totals: dict[str, int] = dict(source.extra.get(_TOTALS_KEY) or {})
        done = set(source.extra.get(_DONE_KEY) or [])
        if not offsets:
            return FetchResult(backfill_done=True)

        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        messages: list[CollectedMessage] = []
        pages = 0
        record_pages = 0
        try:
            sheets = self._sheets
            if not sheets:
                sheets = {str(s.get("id")): s for s in await self._list_sheets(client, source)}
                pages += 1
            budget = max(1, source.max_pages_per_fetch)
            while offsets and record_pages < budget:
                sheet_id = next(iter(offsets))
                sheet = sheets.get(sheet_id)
                if sheet is None or not isinstance(sheet.get("id"), int):
                    offsets.pop(sheet_id)
                    done.add(sheet_id)
                    continue
                cursor = offsets[sheet_id]
                await asyncio.sleep(self._page_delay)
                detail = await self._list_records(client, source, sheet["id"], cursor)
                pages += 1
                record_pages += 1
                messages.extend(self._messages(source, sheet, detail))
                tails[sheet_id] = cursor
                next_offset = detail.get("offset")
                if isinstance(next_offset, str) and next_offset:
                    offsets[sheet_id] = next_offset
                else:
                    offsets.pop(sheet_id)
                    done.add(sheet_id)
                self._report(
                    "backfill",
                    record_pages,
                    budget,
                    len(messages),
                    position=next_offset,
                    detail=str(sheet.get("name") or sheet_id),
                )
        finally:
            if self._owns_client:
                await client.aclose()

        return FetchResult(
            messages=messages,
            pages_fetched=pages,
            state=self._state(offsets, tails, totals, done),
            backfill_done=not offsets,
        )
