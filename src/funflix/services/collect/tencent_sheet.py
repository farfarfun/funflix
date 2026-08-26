"""腾讯文档「智能表格」采集器。

页面本身是 JS 壳，一个字数据都没有。真实数据在 `dop-api/opendoc` 接口里，
且套了三层：**base64url → zlib → 操作日志 JSON**。

一个文档 = 一个 Source。文档下的多个 sheet（tab）由采集器自动枚举，
不需要人工逐个登记。

水位设计与 Telegram 完全不同：表格行 ID 是不透明随机串（`rkxvjg` 这种），
既不单调也没时间戳，无法做行级水位。改用文档版本号 `ver`：
每个 sheet 拉第一个分片就能读到它，与上次相同则整个 sheet 跳过。
行级去重交给 raw_document 的 content_hash。

**脆弱性警告**：这是内部 API，键名全是 `k1`/`k8`/`k30` 这种混淆过的。
腾讯改版会让三层解码中的任意一层静默失败、产出空结果 ——
所以任何一步解不出来都抛异常而不是返回空列表，让失败可见。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import zlib
from datetime import UTC, datetime
from typing import Any

import httpx

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress

logger = logging.getLogger(__name__)

#: 统一到 base.http，避免五个文件各抄一份
_UA = DEFAULT_UA

_API = "https://docs.qq.com/dop-api/opendoc"

#: 从各种写法里取文档 ID：/smartsheet/<id>、/sheet/<id>、/doc/<id>
#: 只认表格路径。**不能**包含 `/doc/` —— 那是文本文档，
#: 结构与编码完全不同，由 TencentTextCollector 负责。
_DOC_PATTERNS = (
    re.compile(r"^https?://docs\.qq\.com/(?:smartsheet|sheet)/(?P<id>[A-Za-z0-9]{8,})", re.I),
)

#: 单元格元数据键，渲染时要跳过
_CELL_META_KEYS = frozenset({"k30", "k31", "k32"})

#: 每片行数。腾讯自己的前端用 60，跟随它以免触发异常检测。
_CHUNK_SIZE = 60

#: Source.extra 里存版本号的键
_STATE_KEY = "tencent_sheet_versions"
#: Source.extra 里存每个 sheet 已扫到第几行的键
_OFFSET_KEY = "tencent_sheet_offsets"
#: Source.extra 里存每个 sheet 总行数的键
_TOTAL_KEY = "tencent_sheet_totals"


class TencentSheetError(RuntimeError):
    """接口结构与预期不符 —— 多半是腾讯改版了。"""


def _decode_smartsheet(payload: dict[str, Any]) -> list[Any]:
    """把 opendoc 响应里的表格数据解出来。

    三层解码的每一层都单独报错，方便定位是哪一层变了。
    """
    try:
        blob = payload["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0][
            "smartsheet"
        ]
    except (KeyError, IndexError, TypeError) as exc:
        raise TencentSheetError(f"响应中找不到 smartsheet 字段（接口可能已改版）：{exc}") from exc

    try:
        raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
    except Exception as exc:
        raise TencentSheetError(f"smartsheet 字段不是合法 base64url：{exc}") from exc

    try:
        decompressed = zlib.decompress(raw)
    except zlib.error as exc:
        raise TencentSheetError(f"smartsheet 数据 zlib 解压失败：{exc}") from exc

    try:
        return json.loads(decompressed)
    except json.JSONDecodeError as exc:
        raise TencentSheetError(f"解压后不是合法 JSON：{exc}") from exc


def parse_page_context(payload: dict[str, Any]) -> dict[str, Any]:
    """取出 `pageCtx`：含 sheet_id、ver（文档版本号）、total_row。"""
    try:
        config = json.loads(payload["clientVars"]["collab_client_vars"]["smartsheetConfig"])
        return json.loads(config["pageCtx"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TencentSheetError(f"读不到 pageCtx：{exc}") from exc


def parse_sheet_ids(payload: dict[str, Any]) -> list[str]:
    """从权限规则里枚举文档下所有 sheet（tab）的 ID。

    这是目前唯一能拿到完整 tab 清单的地方 —— 表格数据本身只含当前 tab。
    """
    try:
        blob = payload["clientVars"]["collab_client_vars"]["smartsheetPermission"]
        rules = json.loads(base64.b64decode(blob + "=" * (-len(blob) % 4)))
    except Exception as exc:
        raise TencentSheetError(
            f"读不到 smartsheetPermission 里的 sheet 清单（接口可能已改版）：{exc}"
        ) from exc

    seen: list[str] = []
    for rule in rules if isinstance(rules, list) else []:
        for entry in (rule.get("priv") or {}).get("items") or []:
            sheet_id = entry.get("sheet_id")
            if sheet_id and sheet_id not in seen:
                seen.append(sheet_id)
    if not seen:
        raise TencentSheetError("权限规则里没有任何 sheet_id")
    return seen


def _collect_cell_text(node: Any, out: list[str]) -> None:
    """递归收集单元格里的文本。

    链接型单元格是 `{k2: 显示文本, k3: href}` —— 取 `k3`。
    显示文本只是展示层，长链接会被截断，没有等于 href 的契约。
    """
    if isinstance(node, dict):
        href = node.get("k3")
        if isinstance(href, str) and href.startswith(("http://", "https://", "magnet:")):
            out.append(href)
            return
        for key, value in node.items():
            if key not in _CELL_META_KEYS:
                _collect_cell_text(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_cell_text(value, out)
    elif isinstance(node, str):
        if node.strip():
            out.append(node.strip())
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.append(str(node))


def cell_to_text(cell: Any) -> str:
    segments: list[str] = []
    _collect_cell_text(cell, segments)
    # 同一单元格里显示文本和 href 可能重复，去重但保序
    deduped = list(dict.fromkeys(segments))
    return " ".join(deduped).strip()


def render_row(columns: dict[str, str], cells: dict[str, Any]) -> str:
    """把一行渲染成 `列名：值` 的文本。

    这是采集层与抽取层之间的约定格式：SheetExtractor 会按它反解回字段，
    RuleExtractor / LLM 抽取器也能直接吃（`剧名：` 本就是标题标记之一）。
    """
    lines: list[str] = []
    for field_id, cell in cells.items():
        value = cell_to_text(cell)
        if not value:
            continue
        label = columns.get(field_id) or field_id
        lines.append(f"{label}：{value}")
    return "\n".join(lines)


def parse_sheet_chunk(ops: list[Any]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """从操作日志里取出「列定义」与「行数据」。

    返回 (字段ID→列名, 行ID→单元格字典)。
    """
    flat = [op for group in ops for op in (group if isinstance(group, list) else [group])]

    columns: dict[str, str] = {}
    rows: dict[str, dict[str, Any]] = {}

    for op in flat:
        if not isinstance(op, dict):
            continue
        content = op.get("c")
        if not isinstance(content, dict):
            continue

        # 列定义：c.k3.k3 = {字段ID: {k30: 列名, k31: 类型}}
        schema = (content.get("k3") or {}).get("k3")
        if isinstance(schema, dict):
            for field_id, meta in schema.items():
                if isinstance(meta, dict) and isinstance(meta.get("k30"), str):
                    columns[field_id] = meta["k30"]

        # 行数据：c.k2.k1 = {行ID: {k1: {字段ID: 单元格}}}
        table = (content.get("k2") or {}).get("k1")
        if isinstance(table, dict):
            for row_id, row in table.items():
                cells = row.get("k1") if isinstance(row, dict) else None
                if isinstance(cells, dict):
                    rows[row_id] = cells

    return columns, rows


class TencentSheetCollector(SupportsProgress):
    name = "tencent-docs-smartsheet-v1"
    #: 先于文本文档问：智能表格的 URL 形如 /sheet/ 或 /smartsheet/，
    #: 比文本文档的模式更具体。
    detect_priority = 10

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        chunk_delay: float = 0.5,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._chunk_delay = chunk_delay

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        candidate = url.strip()
        for pattern in _DOC_PATTERNS:
            match = pattern.match(candidate)
            if match:
                return match.group("id")
        return None

    async def _get(
        self, client: httpx.AsyncClient, doc_id: str, sheet_id: str | None, start: int
    ) -> dict[str, Any]:
        params = {
            "id": doc_id,
            "u": "",
            "noEscape": "1",
            "enableSmartsheetSplit": "1",
            "supportOptimizedVer": "4",
            "chunkCellSize": "15000",
            "enableChunkRank": "1",
            "startrow": str(start),
            "endrow": str(start + _CHUNK_SIZE),
            "normal": "1",
            "outformat": "1",
            "wb": "1",
            "nowb": "0",
            "xsrf": "",
        }
        if sheet_id:
            params["tab"] = sheet_id

        response = await client.get(
            _API,
            params=params,
            headers={
                "User-Agent": _UA,
                "Referer": f"https://docs.qq.com/smartsheet/{doc_id}",
            },
        )
        response.raise_for_status()
        return response.json()

    async def backfill(self, source: Source) -> FetchResult:
        """继续扫剩下的行。

        语义与 Telegram 相反：表格不是「往更早翻」而是「往更后面的行推」。
        `total_row` 已知，低水位就是每个 sheet 已扫到的行偏移，
        推到 `>= total_row` 即该 sheet 补完，全部补完则 `backfill_done`。

        一个 sheet 三万多行、每片 60 行，补完要几百次请求 ——
        所以每轮只推进 `backfill_pages_per_fetch` 片，靠定时任务慢慢磨。
        """
        doc_id = source.identifier
        offsets: dict[str, int] = dict(source.extra.get(_OFFSET_KEY) or {})
        totals: dict[str, int] = dict(source.extra.get(_TOTAL_KEY) or {})

        if not offsets:
            # 还没做过首次采集，没有可续的偏移
            return FetchResult()

        pending = [s for s, off in offsets.items() if off < totals.get(s, 0)]
        if not pending:
            return FetchResult(backfill_done=True)

        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        messages: list[CollectedMessage] = []
        pages = 0
        budget = max(1, source.backfill_pages_per_fetch)

        try:
            for sheet_id in pending:
                start = offsets[sheet_id]
                target = totals.get(sheet_id, 0)
                # 列定义**只随第 0 片下发**，后面每一片都是空的。
                # 不先取一次的话，补历史渲染出来的每一行都是
                # `fn99gF：https://...` 这种原始字段 ID —— 抽取器认不出标题列，
                # 链接全部变成「未归属」。实测线上 482 条未归属资源全部出自这里。
                columns = await self._sheet_columns(client, doc_id, sheet_id)
                if columns:
                    pages += 1

                while pages < budget and start < target:
                    payload = await self._get(client, doc_id, sheet_id, start)
                    pages += 1
                    chunk_columns, rows = parse_sheet_chunk(_decode_smartsheet(payload))
                    # 后续片偶尔也会带列定义（比如中途加了列），合并进来
                    columns = {**columns, **chunk_columns}
                    if not rows:
                        # 空片：可能是接口抖动，也可能 total_row 不准。
                        # 不当成补完，下轮从同一偏移重试。
                        logger.warning(
                            "sheet %s 在第 %d 行处返回空片（共 %s 行）", sheet_id, start, target
                        )
                        break
                    messages.extend(self._to_messages(doc_id, sheet_id, columns, rows))
                    start += _CHUNK_SIZE
                    offsets[sheet_id] = start
                    self._report(
                        "backfill",
                        pages,
                        budget,
                        len(messages),
                        position=f"{start}/{target}",
                        detail=f"sheet {sheet_id}",
                    )
                    if pages < budget and start < target:
                        await asyncio.sleep(self._chunk_delay)
                if pages >= budget:
                    break
        finally:
            if self._owns_client:
                await client.aclose()

        remaining = [s for s, off in offsets.items() if off < totals.get(s, 0)]
        return FetchResult(
            messages=messages,
            pages_fetched=pages,
            state={_OFFSET_KEY: offsets},
            backfill_cursor=str(sum(offsets.values())),
            backfill_done=not remaining,
        )

    async def _sheet_columns(
        self, client: httpx.AsyncClient, doc_id: str, sheet_id: str
    ) -> dict[str, str]:
        """取一个 sheet 的列定义（字段ID → 列名）。

        列定义只随第 0 片下发，非 0 偏移的片一个都不带。追新路径天然从第 0 片
        开始所以没事；补历史是从存下来的偏移接着往下扫的，不单独取一次就永远
        拿不到列名。
        """
        try:
            payload = await self._get(client, doc_id, sheet_id, 0)
            columns, _ = parse_sheet_chunk(_decode_smartsheet(payload))
            return columns
        except Exception as exc:
            # 拿不到列名不该让整轮补历史失败 —— 退化成原始字段 ID，
            # 内容仍然入库，只是抽取质量差一些。
            logger.warning("sheet %s 取列定义失败：%s", sheet_id, exc)
            return {}

    def _to_messages(
        self,
        doc_id: str,
        sheet_id: str,
        columns: dict[str, str],
        rows: dict[str, dict[str, Any]],
    ) -> list[CollectedMessage]:
        now = datetime.now(UTC)
        return [
            CollectedMessage(
                # 行 ID 在文档内唯一但不单调，带上 sheet 前缀以免跨 sheet 撞号
                message_id=f"{sheet_id}:{row_id}",
                text=render_row(columns, cells),
                published_at=now,
                url=f"https://docs.qq.com/smartsheet/{doc_id}?tab={sheet_id}",
            )
            for row_id, cells in rows.items()
        ]

    @staticmethod
    def _merge_offset(existing: int | None, consumed: int) -> int:
        """行偏移只能往前，不能倒退。

        backfill 每轮推进 `backfill_pages_per_fetch` 片，一个三万行的 sheet
        要几百轮才能推到底。而文档一旦追加新行、`ver` 变化，fetch 就会重扫该
        sheet —— 它只读得起最前面几片，直接覆盖就会把 backfill 的进度打回原点，
        之后几百次请求全被 content_hash 去重、白跑，期间新追加的行一直取不到。
        """
        return max(existing or 0, consumed)

    @staticmethod
    def _has_pending_rows(offsets: dict[str, int], totals: dict[str, int]) -> bool:
        """还有没扫到的行吗？用来决定要不要把补历史重新打开。

        `backfill_done` 置 True 之后，全仓库只有 `db reset` 会改回 False。
        文档追加 5000 行后偏移远小于总行数，但补历史因为 done=True 直接返回，
        新行永远采不到 —— 而且每轮采集都显示成功。

        总行数未知时返回 False：拿不到 total 就重开的话，补历史会每轮空跑。
        """
        return any(off < totals.get(sheet_id, 0) for sheet_id, off in offsets.items())

    async def fetch(self, source: Source) -> FetchResult:
        """枚举文档下所有 sheet，拉取版本号变化的那些。"""
        doc_id = source.identifier
        known_versions: dict[str, Any] = dict(source.extra.get(_STATE_KEY) or {})

        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        messages: list[CollectedMessage] = []
        versions: dict[str, Any] = dict(known_versions)
        offsets: dict[str, int] = dict(source.extra.get(_OFFSET_KEY) or {})
        totals: dict[str, int] = dict(source.extra.get(_TOTAL_KEY) or {})
        pages = 0
        truncated = False
        title: str | None = None

        try:
            # 第一次请求既拿 sheet 清单，也拿文档标题
            first = await self._get(client, doc_id, None, 0)
            pages += 1
            sheet_ids = parse_sheet_ids(first)
            title = (first.get("clientVars") or {}).get("title") or None
            logger.info("腾讯文档 %s 共 %d 个 sheet", doc_id, len(sheet_ids))

            budget = max(1, source.max_pages_per_fetch)
            for sheet_id in sheet_ids:
                if pages >= budget:
                    truncated = True
                    break

                payload = await self._get(client, doc_id, sheet_id, 0)
                pages += 1
                context = parse_page_context(payload)
                version = context.get("ver")
                total_row = context.get("total_row")

                if version is not None and known_versions.get(sheet_id) == version:
                    # 版本没变 —— 整个 sheet 跳过，一次请求就判完了
                    logger.debug("sheet %s 版本未变（%s），跳过", sheet_id, version)
                    continue

                if isinstance(total_row, int) and total_row > 0:
                    totals[sheet_id] = total_row

                collected, sheet_pages, sheet_truncated, consumed = await self._collect_sheet(
                    client, doc_id, sheet_id, payload, total_row, budget - pages
                )
                messages.extend(collected)
                pages += sheet_pages
                truncated = truncated or sheet_truncated
                # 记下扫到哪一行，backfill 从这里接着往下扫。
                # 取较大值：backfill 可能已经推得比这一轮远得多，
                # 直接覆盖会把它几百次请求的进度打回原点。
                offsets[sheet_id] = self._merge_offset(offsets.get(sheet_id), consumed)

                # 只有整个 sheet 都取完了才推进版本号，
                # 否则下轮会误以为已同步、跳过剩下的行
                if version is not None and not sheet_truncated:
                    versions[sheet_id] = version
        finally:
            if self._owns_client:
                await client.aclose()

        state: dict[str, Any] = {}
        if versions != known_versions:
            state[_STATE_KEY] = versions
        if offsets:
            state[_OFFSET_KEY] = offsets
        if totals:
            state[_TOTAL_KEY] = totals

        return FetchResult(
            messages=messages,
            pages_fetched=pages,
            truncated=truncated,
            title=title,
            state=state,
            # 还有没扫到的行就要求把补历史重新打开 —— 文档追加新行后，
            # backfill_done 若还停在 True，那批新行永远采不到。
            backfill_pending=self._has_pending_rows(offsets, totals),
        )

    async def _collect_sheet(
        self,
        client: httpx.AsyncClient,
        doc_id: str,
        sheet_id: str,
        first_payload: dict[str, Any],
        total_row: int | None,
        page_budget: int,
    ) -> tuple[list[CollectedMessage], int, bool]:
        columns, rows = parse_sheet_chunk(_decode_smartsheet(first_payload))
        collected: dict[str, dict[str, Any]] = dict(rows)
        pages = 0
        truncated = False

        target = total_row if isinstance(total_row, int) and total_row > 0 else len(rows)
        start = _CHUNK_SIZE
        while len(collected) < target and start < target:
            if pages >= page_budget:
                truncated = True
                break
            await asyncio.sleep(self._chunk_delay)
            payload = await self._get(client, doc_id, sheet_id, start)
            pages += 1
            chunk_columns, chunk_rows = parse_sheet_chunk(_decode_smartsheet(payload))
            columns.update(chunk_columns)
            if not chunk_rows:
                # 还没取到 total_row 就返回了空片：可能是接口抖动，也可能是
                # total_row 不准。无论哪种都**不能**当成"取完了"——
                # 那会推进版本号并永久丢掉剩余的行。标记未完成，下轮重来。
                logger.warning(
                    "sheet %s 在第 %d 行处返回空片（预期共 %s 行），本轮视为未取完",
                    sheet_id,
                    start,
                    target,
                )
                truncated = True
                break
            collected.update(chunk_rows)
            start += _CHUNK_SIZE

        now = datetime.now(UTC)
        messages = [
            CollectedMessage(
                # 行 ID 在文档内唯一但不单调，带上 sheet 前缀以免跨 sheet 撞号
                message_id=f"{sheet_id}:{row_id}",
                text=render_row(columns, cells),
                published_at=now,
                url=f"https://docs.qq.com/smartsheet/{doc_id}?tab={sheet_id}",
            )
            for row_id, cells in collected.items()
        ]
        # 返回**实际扫到的行偏移**而不是页数：空片会让 pages 加一但 start 不动，
        # 拿页数反推偏移就会一次多跳一整片（60 行），那批行再也扫不到。
        return messages, pages, truncated, start
