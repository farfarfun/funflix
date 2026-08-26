"""Telegram 频道采集器。

走公开的 Web 预览页 `https://t.me/s/<channel>`，无需 Bot Token 或 API 凭证。
页面结构（2026-08 实测）：

    <div class="tgme_widget_message ..." data-post="Channel/12345">
      <div class="tgme_widget_message_text js-message_text"> 正文 </div>
      <time datetime="2026-08-25T07:23:07+00:00">
    </div>

翻页靠 `?before=<message_id>` 向更早的消息回溯。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from funflix.base.http import DEFAULT_UA
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult, SupportsProgress

logger = logging.getLogger(__name__)

#: 统一到 base.http，避免五个文件各抄一份
_UA = DEFAULT_UA

#: 追赶模式下，下一轮从哪一页接着往回翻（存在 Source.extra 里）
CATCHUP_BEFORE_KEY = "catchup_before"

#: 从 t.me 的各种 URL 写法里取频道名：t.me/x、t.me/s/x、@x、裸频道名
_CHANNEL_PATTERNS = (
    re.compile(r"^https?://t\.me/s/(?P<name>[A-Za-z0-9_]{3,})", re.I),
    re.compile(r"^https?://t\.me/(?P<name>[A-Za-z0-9_]{3,})", re.I),
    re.compile(r"^@(?P<name>[A-Za-z0-9_]{3,})$"),
    re.compile(r"^(?P<name>[A-Za-z0-9_]{3,})$"),
)

#: 这些标签没有闭合标记，参与深度计数会导致深度永远回不到 0
_VOID_TAGS = frozenset({"br", "img", "hr", "input", "meta", "link", "source", "wbr"})


def _has_class(attrs: dict[str, str], *names: str) -> bool:
    classes = set((attrs.get("class") or "").split())
    return all(n in classes for n in names)


class _ChannelPageParser(HTMLParser):
    """把频道预览页解析成消息列表。

    正文提取有一个不可妥协的点：Telegram 会把长链接**截断显示**
    （锚文本是 `https://pan.quark.cn/s/abc…`，完整地址只在 href 里）。
    所以遇到外链锚点时必须输出 href 而丢弃锚文本，否则入库的就是断链，
    下游无论用正则还是 LLM 都救不回来。
    """

    def __init__(self, channel: str) -> None:
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.messages: list[CollectedMessage] = []
        self.title: str | None = None

        self._msg_id: str | None = None
        self._msg_depth = 0
        self._chunks: list[str] = []
        self._published: str | None = None

        self._text_depth = 0  # >0 表示正在正文 div 内
        # 「回复引用」块：其内容属于被引用的旧消息，不该算进本条正文。
        # 防御性处理 —— 实测样本里没出现过该块，且它可能是 <a> 也可能是 <div>，
        # 故按开启它的标签名来配对闭合，而不是写死 div。
        self._reply_tag: str | None = None
        self._reply_depth = 0
        self._a_suppress = False
        self._a_depth = 0
        self._in_title = False

    # --- 工具 ---

    def _is_external_link(self, href: str) -> bool:
        if not href.lower().startswith(("http://", "https://")):
            return False
        host = (urlparse(href).hostname or "").lower()
        # t.me 内链是 @提及 / 频道跳转，锚文本才是有意义的内容
        return not (host == "t.me" or host.endswith(".t.me"))

    def _emit(self, text: str) -> None:
        if self._text_depth > 0 and self._reply_tag is None and not self._a_suppress:
            self._chunks.append(text)

    def _flush_message(self) -> None:
        if self._msg_id is None:
            return
        text = "".join(self._chunks).strip()
        published = None
        if self._published:
            try:
                published = datetime.fromisoformat(self._published)
            except ValueError:
                logger.debug("无法解析消息时间: %r", self._published)
        self.messages.append(
            CollectedMessage(
                message_id=self._msg_id,
                text=text,
                published_at=published,
                url=f"https://t.me/{self.channel}/{self._msg_id}",
            )
        )
        self._msg_id = None
        self._chunks = []
        self._published = None

    # --- HTMLParser 回调 ---

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}

        if self._a_suppress and tag not in _VOID_TAGS:
            self._a_depth += 1
            return

        if tag == "br":
            self._emit("\n")
            return

        # 消息容器
        if tag == "div" and "data-post" in attrs and _has_class(attrs, "tgme_widget_message"):
            self._flush_message()  # 容错：上一条没正常闭合
            post = attrs["data-post"]
            self._msg_id = post.rsplit("/", 1)[-1]
            self._msg_depth = 1
            return

        if self._msg_id is not None and self._reply_tag is None and tag not in _VOID_TAGS:
            if _has_class(attrs, "tgme_widget_message_reply"):
                self._reply_tag = tag
                self._reply_depth = 1
                if tag == "div":
                    self._msg_depth += 1
                return

        if self._reply_tag is not None:
            if tag == self._reply_tag:
                self._reply_depth += 1
            if tag == "div":
                self._msg_depth += 1
            return

        if self._msg_id is not None and tag == "div":
            self._msg_depth += 1
            if _has_class(attrs, "tgme_widget_message_text"):
                self._text_depth = 1
            elif self._text_depth:
                self._text_depth += 1
            return

        if self._text_depth > 0 and tag == "a":
            href = attrs.get("href", "")
            if self._is_external_link(href):
                self._emit(href)
                self._a_suppress = True
                self._a_depth = 0
            return

        if tag == "time" and "datetime" in attrs and self._msg_id is not None:
            # 取消息内最后一个 time：正文靠后的 meta 块才是发布/编辑时间
            self._published = attrs["datetime"]
            return

        if tag == "div" and _has_class(attrs, "tgme_channel_info_header_title"):
            self._in_title = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._emit("\n")
        else:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._a_suppress:
            if tag == "a" and self._a_depth == 0:
                self._a_suppress = False
            elif tag not in _VOID_TAGS:
                self._a_depth = max(0, self._a_depth - 1)
            return

        if tag == "div" and self._in_title:
            self._in_title = False

        if self._reply_tag is not None:
            if tag == self._reply_tag:
                self._reply_depth -= 1
                if self._reply_depth <= 0:
                    self._reply_tag = None
            if tag == "div" and self._msg_id is not None:
                self._msg_depth -= 1
                if self._msg_depth <= 0:
                    self._flush_message()
            return

        if tag == "div" and self._msg_id is not None:
            if self._text_depth:
                self._text_depth -= 1
            self._msg_depth -= 1
            if self._msg_depth <= 0:
                self._flush_message()

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is None and data.strip():
            self.title = data.strip()
        self._emit(data)

    def close(self) -> None:
        super().close()
        self._flush_message()


def parse_channel_page(html: str, channel: str) -> tuple[list[CollectedMessage], str | None]:
    """解析一页频道预览，返回（消息列表, 频道标题）。消息按 ID 升序。"""
    parser = _ChannelPageParser(channel)
    parser.feed(html)
    parser.close()
    messages = sorted(parser.messages, key=lambda m: m.numeric_id or 0)
    return messages, parser.title


class TelegramChannelCollector(SupportsProgress):
    name = "telegram-web-preview-v1"
    #: 最后问。它的模式能匹配任意裸标识串（"某频道名" 也算命中），
    #: 先问就会把腾讯文档的 URL 一起抢走。
    detect_priority = 900

    def __init__(self, client: httpx.AsyncClient | None = None, page_delay: float = 1.0) -> None:
        self._client = client
        self._owns_client = client is None
        self._page_delay = page_delay

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        candidate = url.strip()
        for pattern in _CHANNEL_PATTERNS:
            m = pattern.match(candidate)
            if m:
                name = m.group("name")
                # t.me/s/x 里的 "s" 是预览路径段，不是频道名
                if name.lower() != "s":
                    return name
        return None

    async def _get_page(self, client: httpx.AsyncClient, channel: str, before: int | None) -> str:
        url = f"https://t.me/s/{channel}"
        params = {"before": str(before)} if before is not None else None
        resp = await client.get(url, params=params, headers={"User-Agent": _UA})
        resp.raise_for_status()
        return resp.text

    async def backfill(self, source: Source) -> FetchResult:
        """往更早的消息回溯。

        从低水位开始用 `?before=` 一路向前翻。返回空页即到顶 ——
        此时置 `backfill_done`，此后每轮只追新，不再往前空跑。

        低水位为空时说明还没做过首次采集，本轮不动，等 fetch 立好水位再说。
        """
        cursor = source.backfill_cursor_id
        if not (cursor and cursor.isdigit()):
            return FetchResult()
        oldest = int(cursor)
        if oldest <= 1:
            return FetchResult(backfill_done=True)

        channel = source.identifier
        client = self._client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        collected: dict[int, CollectedMessage] = {}
        pages = 0
        done = False

        try:
            budget = max(1, source.backfill_pages_per_fetch)
            while pages < budget:
                html = await self._get_page(client, channel, oldest)
                messages, _ = parse_channel_page(html, channel)
                pages += 1

                ids = [m.numeric_id for m in messages if m.numeric_id is not None]
                fresh = [i for i in ids if i < oldest]
                if not fresh:
                    # 没有更早的了 —— 到顶
                    done = True
                    break

                for message in messages:
                    if message.numeric_id is not None and message.numeric_id < oldest:
                        collected[message.numeric_id] = message
                oldest = min(fresh)
                self._report("backfill", pages, budget, len(collected), position=oldest)
                if oldest <= 1:
                    done = True
                    break
                if pages < budget:
                    await asyncio.sleep(self._page_delay)
        finally:
            if self._owns_client:
                await client.aclose()

        return FetchResult(
            messages=[collected[k] for k in sorted(collected)],
            pages_fetched=pages,
            backfill_cursor=str(oldest),
            backfill_done=done,
        )

    async def fetch(self, source: Source) -> FetchResult:
        """取回水位之后的所有消息。

        从最新页开始，按 `?before=` 逐页向更早回溯，直到追上水位。
        首次采集（无水位）只取一页 —— 否则接入一个老频道会把整个历史拉下来。

        **追赶模式**：上一轮页数用完（`truncated`）时，会把停在哪一页记进
        `extra[CATCHUP_BEFORE_KEY]`，这一轮从那里接着往回翻，而不是又从最新页重来。
        不接着翻的话，每轮都在重采最新的那几页、永远够不到中间那段，
        停机一天就等于丢一天 —— 而且每轮都"采集成功"，看不出任何异常。
        """
        channel = source.identifier
        cursor = None
        if source.cursor_message_id and source.cursor_message_id.isdigit():
            cursor = int(source.cursor_message_id)

        resume = (source.extra or {}).get(CATCHUP_BEFORE_KEY)
        resume_before = int(resume) if str(resume or "").isdigit() else None

        client = self._client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        collected: dict[int, CollectedMessage] = {}
        pages = 0
        truncated = False
        title: str | None = None
        before: int | None = resume_before

        try:
            while pages < max(1, source.max_pages_per_fetch):
                html = await self._get_page(client, channel, before)
                messages, page_title = parse_channel_page(html, channel)
                pages += 1
                title = title or page_title

                if not messages:
                    break

                ids = [m.numeric_id for m in messages if m.numeric_id is not None]
                if not ids:
                    break

                for m in messages:
                    if m.numeric_id is None:
                        continue
                    if cursor is None or m.numeric_id > cursor:
                        collected[m.numeric_id] = m

                oldest = min(ids)
                if cursor is None:
                    # 首次接入：只取最新一页，把水位立在这里
                    break
                if oldest <= cursor:
                    # 本页已经跨过水位，说明新消息取全了
                    break

                before = oldest
                self._report(
                    "fetch", pages, source.max_pages_per_fetch, len(collected), position=oldest
                )
                if pages < source.max_pages_per_fetch:
                    await asyncio.sleep(self._page_delay)
            else:
                truncated = True
        finally:
            if self._owns_client:
                await client.aclose()

        return FetchResult(
            messages=[collected[k] for k in sorted(collected)],
            pages_fetched=pages,
            truncated=truncated,
            title=title,
            # 追赶未完成就记下停在哪；完成了就把这个键清掉（None = 清除）
            state={CATCHUP_BEFORE_KEY: str(before) if truncated and before else None},
        )
