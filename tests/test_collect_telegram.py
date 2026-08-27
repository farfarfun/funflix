from __future__ import annotations

import httpx
import pytest
from factories import build_message, build_page, simple_page

from funflix.base.enums import SourceType
from funflix.models import Source
from funflix.services.collect import telegram
from funflix.services.collect.registry import detect_source
from funflix.services.collect.telegram import TelegramChannelCollector, parse_channel_page

CHANNEL = "TestChannel"


class TestNormalizeIdentifier:
    @pytest.mark.parametrize(
        "url",
        [
            "https://t.me/s/Quark_Movies",
            "https://t.me/Quark_Movies",
            "http://t.me/s/Quark_Movies",
            "@Quark_Movies",
            "Quark_Movies",
        ],
    )
    def test_accepts_all_channel_url_forms(self, url: str) -> None:
        assert TelegramChannelCollector.normalize_identifier(url) == "Quark_Movies"

    @pytest.mark.parametrize("url", ["https://example.com/x", "https://t.me/", "ab"])
    def test_rejects_non_channel_urls(self, url: str) -> None:
        assert TelegramChannelCollector.normalize_identifier(url) is None

    def test_detect_source_maps_to_telegram(self) -> None:
        assert detect_source("https://t.me/s/Quark_Movies") == (
            SourceType.TELEGRAM,
            "Quark_Movies",
        )


class TestParseChannelPage:
    def test_extracts_id_text_and_time(self) -> None:
        html = simple_page(CHANNEL, [101, 102])
        messages, title = parse_channel_page(html, CHANNEL)

        assert [m.message_id for m in messages] == ["101", "102"]
        assert title == "测试频道"
        assert messages[0].url == f"https://t.me/{CHANNEL}/101"
        assert messages[0].published_at is not None
        assert messages[0].published_at.tzinfo is not None

    def test_sorts_by_id_ascending(self) -> None:
        html = simple_page(CHANNEL, [105, 101, 103])
        messages, _ = parse_channel_page(html, CHANNEL)
        assert [m.message_id for m in messages] == ["101", "103", "105"]

    def test_br_becomes_newline(self) -> None:
        html = build_page(CHANNEL, [build_message(CHANNEL, 1, "第一行<br/>第二行<br>第三行")])
        messages, _ = parse_channel_page(html, CHANNEL)
        assert messages[0].text == "第一行\n第二行\n第三行"

    def test_external_anchor_yields_href_not_display_text(self) -> None:
        """锚文本只是展示层，没有等于 href 的契约；一律以 href 为准。"""
        html = build_page(
            CHANNEL,
            [
                build_message(
                    CHANNEL,
                    1,
                    '链接：<a href="https://pan.quark.cn/s/full0123456789">'
                    "https://pan.quark.cn/s/full…</a>",
                )
            ],
        )
        messages, _ = parse_channel_page(html, CHANNEL)
        assert "https://pan.quark.cn/s/full0123456789" in messages[0].text
        assert "…" not in messages[0].text

    def test_internal_tme_anchor_keeps_display_text(self) -> None:
        """@提及 / 频道跳转的锚文本才是有意义的内容，不该被 href 覆盖。"""
        html = build_page(
            CHANNEL,
            [build_message(CHANNEL, 1, '关注 <a href="https://t.me/SomeChannel">@SomeChannel</a>')],
        )
        messages, _ = parse_channel_page(html, CHANNEL)
        assert "@SomeChannel" in messages[0].text
        assert "https://t.me/SomeChannel" not in messages[0].text

    def test_relative_hashtag_anchor_keeps_text(self) -> None:
        html = build_page(
            CHANNEL, [build_message(CHANNEL, 1, '标签 <a href="?q=%23剧情">#剧情</a>')]
        )
        messages, _ = parse_channel_page(html, CHANNEL)
        assert "#剧情" in messages[0].text

    def test_nested_tags_inside_text_are_flattened(self) -> None:
        html = build_page(
            CHANNEL,
            [build_message(CHANNEL, 1, '名称：<b>剧集</b><i class="emoji"><b>📁</b></i>结束')],
        )
        messages, _ = parse_channel_page(html, CHANNEL)
        assert messages[0].text == "名称：剧集📁结束"

    def test_reply_quote_is_excluded_from_text(self) -> None:
        reply = (
            '<a class="tgme_widget_message_reply" href="https://t.me/x/1">'
            '<div class="tgme_widget_message_text">被引用的旧消息</div></a>'
        )
        html = build_page(CHANNEL, [build_message(CHANNEL, 1, "本条正文", reply=reply)])
        messages, _ = parse_channel_page(html, CHANNEL)
        assert messages[0].text == "本条正文"

    def test_message_without_text_yields_empty_string(self) -> None:
        html = build_page(
            CHANNEL,
            [
                '<div class="tgme_widget_message js-widget_message" '
                f'data-post="{CHANNEL}/7"><div class="tgme_widget_message_bubble">'
                '<time datetime="2026-08-25T07:23:07+00:00"></time></div></div>'
            ],
        )
        messages, _ = parse_channel_page(html, CHANNEL)
        assert messages[0].message_id == "7"
        assert messages[0].text == ""

    def test_empty_page_yields_nothing(self) -> None:
        messages, _ = parse_channel_page(build_page(CHANNEL, []), CHANNEL)
        assert messages == []


def _mock_collector(pages: dict[int | None, str]) -> TelegramChannelCollector:
    """按 ?before= 参数返回预置页面。"""
    requested: list[int | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.url.params.get("before")
        before = int(raw) if raw else None
        requested.append(before)
        return httpx.Response(200, text=pages.get(before, build_page(CHANNEL, [])))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = TelegramChannelCollector(client=client, page_delay=0)
    collector.requested = requested  # type: ignore[attr-defined]
    return collector


def _source(
    cursor: str | None = None, max_pages: int = 5, backfill_cursor: str | None = None
) -> Source:
    return Source(
        id=1,
        source_type=SourceType.TELEGRAM,
        url=f"https://t.me/s/{CHANNEL}",
        identifier=CHANNEL,
        max_pages_per_fetch=max_pages,
        cursor_message_id=cursor,
        backfill_cursor_id=backfill_cursor,
    )


@pytest.mark.asyncio
class TestFetch:
    async def test_first_fetch_takes_only_latest_page(self) -> None:
        """无水位时只取一页 —— 否则接入老频道会把整个历史拉下来。"""
        collector = _mock_collector({None: simple_page(CHANNEL, [101, 102, 103])})
        result = await collector.fetch(_source(cursor=None))

        assert [m.message_id for m in result.messages] == ["101", "102", "103"]
        assert result.pages_fetched == 1
        assert result.truncated is False

    async def test_returns_only_messages_after_cursor(self) -> None:
        collector = _mock_collector({None: simple_page(CHANNEL, [101, 102, 103, 104])})
        result = await collector.fetch(_source(cursor="102"))

        assert [m.message_id for m in result.messages] == ["103", "104"]

    async def test_pages_backwards_until_reaching_cursor(self) -> None:
        collector = _mock_collector(
            {
                None: simple_page(CHANNEL, [110, 111, 112]),
                110: simple_page(CHANNEL, [107, 108, 109]),
                107: simple_page(CHANNEL, [104, 105, 106]),
            }
        )
        result = await collector.fetch(_source(cursor="105"))

        assert [m.message_id for m in result.messages] == [
            "106",
            "107",
            "108",
            "109",
            "110",
            "111",
            "112",
        ]
        assert collector.requested == [None, 110, 107]  # type: ignore[attr-defined]
        assert result.truncated is False

    async def test_flags_truncated_when_hitting_page_limit(self) -> None:
        collector = _mock_collector(
            {
                None: simple_page(CHANNEL, [110, 111]),
                110: simple_page(CHANNEL, [108, 109]),
            }
        )
        result = await collector.fetch(_source(cursor="1", max_pages=2))

        assert result.truncated is True
        assert result.pages_fetched == 2

    async def test_stops_on_empty_page(self) -> None:
        collector = _mock_collector({None: build_page(CHANNEL, [])})
        result = await collector.fetch(_source(cursor="100"))

        assert result.messages == []
        assert result.pages_fetched == 1


@pytest.mark.asyncio
class TestBackfill:
    async def test_stops_at_top_and_marks_done(self) -> None:
        collector = _mock_collector(
            {
                200: simple_page(CHANNEL, [107, 108, 109]),
                107: simple_page(CHANNEL, [1, 2, 3]),
            }
        )
        result = await collector.backfill(_source(backfill_cursor="200"))

        assert result.backfill_done is True
        assert result.backfill_cursor == "1"

    async def test_stops_after_page_cap_leaves_backfill_open(self, monkeypatch) -> None:
        """频道历史很长时不能无限翻——单次撞到页数上限就地收工，下次接着翻。"""
        monkeypatch.setattr(telegram, "_MAX_BACKFILL_PAGES_PER_RUN", 3)

        def handler(request: httpx.Request) -> httpx.Response:
            raw = request.url.params.get("before")
            before = int(raw)
            ids = [before - 3, before - 2, before - 1]
            return httpx.Response(200, text=simple_page(CHANNEL, ids))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = TelegramChannelCollector(client=client, page_delay=0)

        result = await collector.backfill(_source(backfill_cursor="1000"))

        assert result.backfill_done is False
        assert result.pages_fetched == 3
        # 每页往前挪 3 个 id：1000 -> 997 -> 994 -> 991
        assert result.backfill_cursor == "991"
