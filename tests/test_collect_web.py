from __future__ import annotations

import hashlib

import httpx
import pytest

from funflix.base.enums import Provider, SourceType
from funflix.models import Source
from funflix.services.collect.registry import detect_source
from funflix.services.collect.web import WebCollector
from funflix.services.extract.rule import RuleExtractor


@pytest.mark.asyncio
async def test_collects_direct_links_and_torrent_attachments() -> None:
    torrent_info = b"d4:name9:Movie.mkve"
    torrent = b"d4:info" + torrent_info + b"e"
    retry_failed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal retry_failed
        if request.url.path == "/":
            return httpx.Response(
                200,
                text=(
                    '<a href="/broken/9.html">失效置顶</a>'
                    '<a href="/bd/20260906/1.html">[夸克下载][电影甲][4K]</a>'
                    '<a href="/donghuapian/3.html">电影丙[国英/中字]</a>'
                    '<a href="/thread-2.htm">[BT下载][电影乙][1080p]</a>'
                    '<a href="/retry/8.html">电影丁</a>'
                    '<a href="/about.html">关于</a>'
                ),
            )
        if request.url.path == "/broken/9.html":
            return httpx.Response(404)
        if request.url.path == "/bd/20260906/1.html":
            return httpx.Response(
                200,
                text=(
                    "<title>【电影甲】下载,迅雷下载-66影视</title>"
                    '<a href="https://pan.quark.cn/s/share1">夸克</a>'
                ),
            )
        if request.url.path == "/donghuapian/3.html":
            return httpx.Response(
                200,
                text=(
                    "<title>【电影丙】下载-66影视</title>"
                    '<a href="magnet:?xt=urn:btih:'
                    '0123456789abcdef0123456789abcdef01234567">磁力</a>'
                ),
            )
        if request.url.path == "/thread-2.htm":
            return httpx.Response(
                200,
                text=(
                    "<title>[BT下载][电影乙][1080p]-最新电影-BT之家</title>"
                    '<a href="/attach-download-2.htm">Movie.torrent</a>'
                ),
            )
        if request.url.path == "/retry/8.html":
            if not retry_failed:
                retry_failed = True
                raise httpx.ReadTimeout("temporary", request=request)
            return httpx.Response(
                200,
                text=(
                    "<title>电影丁-新版6v电影（旧版66影视）- 免费电影下载</title>"
                    '<a href="https://pan.quark.cn/s/share2">夸克</a>'
                ),
            )
        return httpx.Response(200, content=torrent)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = WebCollector(client)
    source = Source(
        id=1,
        source_type=SourceType.WEB,
        url="https://www.66yingshi.com/",
        identifier="https://www.66yingshi.com/",
        max_pages_per_fetch=5,
        extra={},
    )

    first = await collector.fetch(source)
    source.extra = first.state
    second = await collector.fetch(source)
    source.extra = second.state
    third = await collector.fetch(source)
    await client.aclose()

    outcomes = [await RuleExtractor().extract(message.text) for message in first.messages]
    assert detect_source(source.url) == (SourceType.WEB, source.url)
    assert [outcome.items[0].title for outcome in outcomes] == ["电影甲", "电影丙", "电影乙"]
    assert [outcome.items[0].links[0].provider for outcome in outcomes] == [
        Provider.QUARK,
        Provider.MAGNET,
        Provider.MAGNET,
    ]
    assert outcomes[2].items[0].links[0].share_id == hashlib.sha1(torrent_info).hexdigest()
    assert first.truncated is True
    assert second.messages[0].text.startswith("名称：电影丁")
    assert third.messages == []
