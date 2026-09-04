from __future__ import annotations

import httpx
import pytest

from funflix.base.enums import SourceType
from funflix.models import Source
from funflix.services.collect.registry import detect_source
from funflix.services.collect.rss import RSSCollector, parse_feed

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<title>测试资源</title><item><guid>a</guid><title>剧集甲</title>
<description><![CDATA[链接：<a href="https://pan.quark.cn/s/abc123">点击</a>]]></description>
<pubDate>Fri, 04 Sep 2026 04:21:27 +0000</pubDate></item>
<item><guid>b</guid><title>动漫乙</title><nyaa:infoHash
xmlns:nyaa="https://nyaa.si/xmlns/nyaa">ABCDEF0123456789ABCDEF0123456789ABCDEF01</nyaa:infoHash></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
<entry><id>x</id><title>条目</title><link href="https://example.test/a"/><updated>2026-09-04T04:21:27Z</updated></entry></feed>"""


def _source(extra: dict | None = None) -> Source:
    return Source(
        id=1,
        source_type=SourceType.RSS,
        url="https://nyaa.si/?page=rss&c=1_2&f=0",
        identifier="https://nyaa.si/?page=rss&c=1_2&f=0",
        extra=extra or {},
    )


def test_detects_feed_urls_without_claiming_arbitrary_pages() -> None:
    assert detect_source("https://nyaa.si/?page=rss&c=1_2") == (
        SourceType.RSS,
        "https://nyaa.si/?page=rss&c=1_2",
    )
    assert detect_source("https://example.com/whatever") is None


def test_parse_rss_keeps_links_and_builds_magnet() -> None:
    messages, title = parse_feed(RSS)
    assert title == "测试资源"
    assert {message.message_id for message in messages} == {"a", "b"}
    assert "https://pan.quark.cn/s/abc123" in messages[0].text
    assert "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01" in messages[1].text


def test_parse_atom() -> None:
    messages, title = parse_feed(ATOM)
    assert title == "Atom"
    assert messages[0].url == "https://example.test/a"


@pytest.mark.asyncio
async def test_fetch_is_incremental() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=RSS.encode()))
    )
    collector = RSSCollector(client=client)
    first = await collector.fetch(_source())
    second = await collector.fetch(_source(first.state))
    await client.aclose()
    assert len(first.messages) == 2
    assert second.messages == []
    assert set(first.state["rss_seen_ids"]) == {"a", "b"}
