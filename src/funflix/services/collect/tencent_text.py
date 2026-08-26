"""腾讯文档「文本文档」采集器（padType=doc）。

与 `tencent_sheet.py`（智能表格）是两回事，两者同域名但格式毫无共同之处：

> 注意 `SourceType.TENCENT_DOC` 的**枚举值**仍是历史命名 `"tencent_doc"`
> （表格那个是 `"tencent_docs"`，只差一个 s）。值落在 source 与 raw_document
> 两张表里，改了库里已有的行就再也匹配不上采集器，那些源会静默停止采集，
> 所以只改了模块与类名。想让两边一致得配一次数据迁移。

| | smartsheet | doc |
|---|---|---|
| 正文位置 | `initialAttributedText.text[0].smartsheet` | `...text[0].commands` |
| 编码 | base64url → zlib → 操作日志 | 明文 JSON |
| 结构 | 行/列 | 一串富文本变更（mutations） |
| 抽取器 | sheet（按列映射） | rule（自由文本分段） |

正文重建规则（2026-08 实测）：

- `ty=="is"` 的 mutation 携带全文，拼接即可
- `\\r` 是段落分隔
- 超链接是 RTF 风格字段码：``\\x13HYPERLINK <url> <选项>\\x14<显示文本>\\x15``

**取 url 而不是显示文本**：显示文本是展示层，常常是「点击查看」之类，
或者被截断的地址。锚文本没有等于 href 的契约 —— 这条在 Telegram 采集器上
已经踩过一次，这里直接按结论写。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress

logger = logging.getLogger(__name__)

_API = "https://docs.qq.com/dop-api/opendoc"
#: 统一到 base.http，避免五个文件各抄一份
_UA = DEFAULT_UA

_DOC_PATTERNS = (re.compile(r"^https?://docs\.qq\.com/doc/(?P<id>[A-Za-z0-9]{8,})", re.I),)

#: 字段码：\x13 HYPERLINK <url> <选项> \x14 <显示文本> \x15
_FIELD_RE = re.compile("\x13\\s*HYPERLINK\\s+(?P<url>\\S+)[^\x14]*\x14(?P<label>[^\x15]*)\x15")
#: 没有 HYPERLINK 的其它字段码（书签、目录等），整体丢弃
_OTHER_FIELD_RE = re.compile("\x13[^\x14]*\x14([^\x15]*)\x15")

#: Source.extra 里存文档版本的键
_STATE_KEY = "tencent_doc_rev"

#: 单个段落块的字符上限。超长块多半是没有空行分隔的长列表，
#: 硬切开以免单条 raw_document 大到把 LLM 抽取撑爆。
_MAX_BLOCK_CHARS = 4000


class TencentTextError(RuntimeError):
    """响应结构与预期不符 —— 多半是腾讯改版了。"""


def extract_plain_text(payload: dict[str, Any]) -> str:
    """把 opendoc 响应还原成带链接的纯文本。"""
    try:
        commands = payload["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0][
            "commands"
        ]
    except (KeyError, IndexError, TypeError) as exc:
        raise TencentTextError(f"响应里找不到 commands（接口可能已改版）：{exc}") from exc

    chunks: list[str] = []
    for command in commands if isinstance(commands, list) else []:
        for mutation in (command or {}).get("mutations") or []:
            if mutation.get("ty") == "is" and isinstance(mutation.get("s"), str):
                chunks.append(mutation["s"])

    if not chunks:
        raise TencentTextError("commands 里没有任何文本内容")

    text = "".join(chunks)
    # 超链接字段码 → 「显示文本 url」，两者都留：显示文本常含剧名
    text = _FIELD_RE.sub(lambda m: f"{m.group('label')} {m.group('url')}", text)
    # 其余字段码只保留显示文本
    text = _OTHER_FIELD_RE.sub(lambda m: m.group(1), text)
    # 段落分隔统一成换行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 残留的控制符（\x08 \x0f \x1e 等）会污染 content_hash，清掉
    return "".join(ch for ch in text if ch >= " " or ch == "\n")


def split_blocks(text: str, max_chars: int = _MAX_BLOCK_CHARS) -> list[str]:
    """按空行把全文切成块，每块将成为一条 raw_document。

    空行是作者自己的分隔意图，比按固定行数硬切更贴近语义边界。
    没有空行的超长块再按 max_chars 兜底切开。
    """
    blocks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        chunk = "\n".join(current).strip()
        current.clear()
        if not chunk:
            return
        while len(chunk) > max_chars:
            cut = chunk.rfind("\n", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            blocks.append(chunk[:cut].strip())
            chunk = chunk[cut:].strip()
        if chunk:
            blocks.append(chunk)

    for line in text.split("\n"):
        if line.strip():
            current.append(line.rstrip())
        else:
            flush()
    flush()
    return blocks


class TencentTextCollector(SupportsProgress):
    name = "tencent-doc-v1"
    #: 排在智能表格之后 —— 两者同域名，先问更具体的那个。
    detect_priority = 20

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        for pattern in _DOC_PATTERNS:
            match = pattern.match(url.strip())
            if match:
                return match.group("id")
        return None

    async def _get(self, client: httpx.AsyncClient, doc_id: str) -> dict[str, Any]:
        response = await client.get(
            _API,
            params={
                "id": doc_id,
                "normal": "1",
                "outformat": "1",
                "wb": "1",
                "nowb": "0",
                "xsrf": "",
            },
            headers={"User-Agent": _UA, "Referer": f"https://docs.qq.com/doc/{doc_id}"},
        )
        response.raise_for_status()
        return response.json()

    async def fetch(self, source: Source) -> FetchResult:
        """整篇拉取。

        文本文档没有分页接口，一次就是全量（实测 26MB / 1.6 万段）。
        所以用文档版本号 `rev` 当水位：没变就整篇跳过，避免每轮重算 1.6 万个 hash。
        """
        doc_id = source.identifier
        known_rev = source.extra.get(_STATE_KEY)

        client = self._client or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        try:
            payload = await self._get(client, doc_id)
        finally:
            if self._owns_client:
                await client.aclose()

        collab = (payload.get("clientVars") or {}).get("collab_client_vars") or {}
        rev = collab.get("rev")
        title = (payload.get("bodyData") or {}).get("initialTitle") or None

        if collab.get("isChunked"):
            # 大文档腾讯会分块返回，而本采集器只发一次请求。
            # 继续跑会拿着半份内容当全量、还推进版本号，属于静默丢数据 ——
            # 宁可显式失败。分块协议尚未逆向，见 docs/DESIGN.md。
            raise TencentTextError(
                f"文档 {doc_id} 是分块返回的（isChunked=true），当前采集器只支持单次全量拉取。"
                f"继续处理会静默丢失内容，故中止。"
            )

        if rev is not None and known_rev == rev:
            logger.debug("腾讯文档 %s 版本未变（rev=%s），跳过", doc_id, rev)
            return FetchResult(pages_fetched=1, title=title, backfill_done=True)

        blocks = split_blocks(extract_plain_text(payload))
        now = datetime.now(UTC)
        logger.info("腾讯文档 %s 切出 %d 个段落块", doc_id, len(blocks))

        messages = [
            CollectedMessage(
                # 文本文档没有稳定的行 ID，用序号；内容变化由 content_hash 兜底去重
                message_id=f"{doc_id}:{index}",
                text=block,
                published_at=now,
                url=f"https://docs.qq.com/doc/{doc_id}",
            )
            for index, block in enumerate(blocks)
        ]

        return FetchResult(
            messages=messages,
            pages_fetched=1,
            title=title,
            state={_STATE_KEY: rev} if rev is not None else {},
            # 一次即全量，没有"更早的历史"可补
            backfill_done=True,
        )

    async def backfill(self, source: Source) -> FetchResult:
        """文本文档一次就是全量，没有可回溯的历史。"""
        return FetchResult(backfill_done=True)
