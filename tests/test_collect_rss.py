from __future__ import annotations

import httpx
import pytest

from funflix.base.enums import SourceType
from funflix.models import Source
from funflix.services.collect.registry import detect_source
from funflix.services.collect.rss import RSSCollector, parse_feed
from funflix.services.text.linkscan import scan_links

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<title>测试资源</title><item><guid>a</guid><title>剧集甲</title>
<description><![CDATA[链接：<a href="https://pan.quark.cn/s/abc123">点击</a>]]></description>
<enclosure url="https://example.test/a.torrent" type="application/x-bittorrent" />
<pubDate>Fri, 04 Sep 2026 04:21:27 +0000</pubDate></item>
<item><guid>b</guid><title>动漫乙</title><nyaa:infoHash
xmlns:nyaa="urn:example:torrent">ABCDEF0123456789ABCDEF0123456789ABCDEF01</nyaa:infoHash>
<torrent:magnetURI xmlns:torrent="http://xmlns.ezrss.it/0.1/"><![CDATA[
magnet:?xt=urn:btih:1234567890ABCDEF1234567890ABCDEF12345678
]]></torrent:magnetURI></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
<entry><id>x</id><title>条目</title><link href="https://example.test/a"/><updated>2026-09-04T04:21:27Z</updated></entry></feed>"""


def _source(extra: dict | None = None) -> Source:
    return Source(
        id=1,
        source_type=SourceType.RSS,
        url="https://feeds.example/latest.rss?category=translated",
        identifier="https://feeds.example/latest.rss?category=translated",
        extra=extra or {},
    )


def test_detects_feed_urls_before_generic_web_pages() -> None:
    assert detect_source("https://feeds.example/latest.rss?category=translated") == (
        SourceType.RSS,
        "https://feeds.example/latest.rss?category=translated",
    )
    assert detect_source("https://example.com/whatever") == (
        SourceType.WEB,
        "https://example.com/whatever",
    )


def test_parse_rss_keeps_links_and_builds_magnet() -> None:
    messages, title = parse_feed(RSS)
    assert title == "测试资源"
    assert {message.message_id for message in messages} == {"a", "b"}
    assert scan_links(messages[0].text)[0].url == "https://pan.quark.cn/s/abc123"
    assert "https://example.test/a.torrent" in messages[0].text
    assert "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01" in messages[1].text
    assert "magnet:?xt=urn:btih:1234567890ABCDEF1234567890ABCDEF12345678" in messages[1].text


def test_parse_atom() -> None:
    messages, title = parse_feed(ATOM)
    assert title == "Atom"
    assert messages[0].url == "https://example.test/a"


def test_parse_gb2312_feed() -> None:
    payload = (
        '<?xml version="1.0" encoding="gb2312"?>'
        "<rss><channel><title>影视更新</title><item><guid>1</guid><title>剧集甲</title>"
        "<description>https://pan.quark.cn/s/abc</description></item></channel></rss>"
    ).encode("gb18030")
    messages, title = parse_feed(payload)
    assert title == "影视更新"
    assert messages[0].message_id == "1"


def test_parse_feed_ignores_whitespace_before_xml_declaration() -> None:
    messages, title = parse_feed(
        b'\n \n<?xml version="1.0" encoding="UTF-8"?>'
        b"<rss><channel><title>Feed</title><item><guid>1</guid><title>Item</title>"
        b"</item></channel></rss>"
    )
    assert title == "Feed"
    assert messages[0].message_id == "1"


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
