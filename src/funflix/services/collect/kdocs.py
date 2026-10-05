"""金山文档多维表格采集器。"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from farlog import getLogger

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress

logger = getLogger("funflix")

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
        """递归收集单元格内的文本片段：优先取 `address`（超链接显示文本），否则深入字典/列表。"""
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
    """把一条多维表格记录渲染成 `字段名：值` 按行拼接的文本。

    Args:
        fields: 记录的字段名到原始单元格值的映射。
        ignored_fields: 要跳过的字段名（如附件类型字段，其值不是可读文本）。

    Returns:
        渲染后的多行文本；值为空的字段不会单独成行。
    """
    lines: list[str] = []
    for label, cell in fields.items():
        if ignored_fields and label in ignored_fields:
            continue
        value = cell_to_text(cell)
        if value:
            lines.append(f"{label}：{value}")
    return "\n".join(lines)


class KDocsCollector(SupportsProgress):
    """金山文档多维表格（kdocs.cn 的表格类文档）采集器。

    多维表格可以有多个 sheet，每个 sheet 的行 ID 不单调（会被用户随时插入/
    删除），没法像 Telegram 那样用单一水位判断"有没有新内容"，所以水位状态
    按 sheet 分别记录：`_OFFSETS_KEY`（未读完的翻页游标）、`_TAILS_KEY`（已
    读完的 sheet 下次续读的起点）、`_TOTALS_KEY`（上次看到的行数，行数增长
    说明有新增行）、`_DONE_KEY`（已经读到表尾的 sheet 集合）。
    """

    name = "kdocs-database-v1"
    detect_priority = 15

    def __init__(self, client: httpx.AsyncClient | None = None, page_delay: float = 0.1) -> None:
        """初始化采集器。

        Args:
            client: 复用的 `httpx.AsyncClient`；为 None 时按次创建并在用完后关闭。
            page_delay: 补历史翻页之间的等待秒数，避免请求过于密集。
        """
        self._client = client
        self._owns_client = client is None
        self._page_delay = page_delay
        #: `fetch()` 里拉到的 sheet 元信息缓存，供同一轮 `backfill()` 复用，
        #: 避免重复请求 `listSheets`。
        self._sheets: dict[str, dict[str, Any]] = {}

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        """从 kdocs.cn 分享链接提取文档 ID。

        Args:
            url: 待识别的地址，需形如 `https://www.kdocs.cn/l/<id>`。

        Returns:
            识别成功时返回文档 ID；不匹配时返回 None。
        """
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

    async def _list_sheets(self, client: httpx.AsyncClient, source: Source) -> list[dict[str, Any]]:
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
        """继续翻 `fetch()` 遗留下来的未读完 sheet，直到翻完或撞到页数预算。

        每轮最多翻 `source.max_pages_per_fetch` 页记录（跨 sheet 累计），一个
        sheet 翻完就从 `offsets` 里摘掉、标记进 `done`，再轮到下一个未读完的
        sheet；全部摘完后 `backfill_done=True`。

        Args:
            source: 待补历史的 KDocs 源。

        Returns:
            本轮翻页取到的消息及更新后的翻页状态；若上轮没有遗留的未读 sheet，
            直接返回空结果并置 `backfill_done=True`。
        """
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
