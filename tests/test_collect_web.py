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
                    "<article onclick=\"window.location.href='/thread-2.htm'\">电影乙</article>"
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
                    "<title>【电影甲】下载,迅雷下载-影视站</title>"
                    '<a href="https://pan.quark.cn/s/share1">夸克</a>'
                ),
            )
        if request.url.path == "/donghuapian/3.html":
            return httpx.Response(
                200,
                text=(
                    "<title>【电影丙】下载-影视站</title>"
                    '<a href="magnet:?xt=urn:btih:'
                    '0123456789abcdef0123456789abcdef01234567">磁力</a>'
                ),
            )
        if request.url.path == "/thread-2.htm":
            return httpx.Response(
                200,
                text=(
                    "<title>[BT下载][电影乙][1080p]-最新电影-资源论坛</title>"
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
                    "<title>电影丁-免费电影下载</title>"
                    '<a href="https://pan.quark.cn/s/share2">夸克</a>'
                ),
            )
        return httpx.Response(200, content=torrent)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = WebCollector(client)
    source = Source(
        id=1,
        source_type=SourceType.WEB,
        url="https://media.example/",
        identifier="https://media.example/",
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


@pytest.mark.asyncio
async def test_collects_multiple_titles_with_one_shared_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/collections":
            return httpx.Response(200, text='<a href="/daily/2026-09-07">今日更新</a>')
        return httpx.Response(
            200,
            text=(
                "<title>今日合集</title>"
                "<p>1. 剧集甲</p><p>2. 剧集乙</p>"
                '<a href="https://pan.quark.cn/s/shared">合集地址</a>'
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        id=1,
        source_type=SourceType.WEB,
        url="https://media.example/collections",
        identifier="https://media.example/collections",
        max_pages_per_fetch=5,
        extra={"detail_pattern": r"/daily/\d{4}-\d{2}-\d{2}$"},
    )
    result = await WebCollector(client).fetch(source)
    await client.aclose()

    outcome = await RuleExtractor().extract(result.messages[0].text)
    assert [item.title for item in outcome.items] == ["剧集甲", "剧集乙"]
    assert all(item.links[0].provider is Provider.QUARK for item in outcome.items)


@pytest.mark.asyncio
async def test_keeps_one_link_per_title_separate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/collections":
            return httpx.Response(200, text='<a href="/daily/1.html">今日更新</a>')
        return httpx.Response(
            200,
            text=(
                '<p>1. 剧集甲 <a href="https://pan.quark.cn/s/one">地址</a></p>'
                '<p>2. 剧集乙 <a href="https://pan.quark.cn/s/two">地址</a></p>'
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        id=1,
        source_type=SourceType.WEB,
        url="https://media.example/collections",
        identifier="https://media.example/collections",
        max_pages_per_fetch=5,
        extra={},
    )
    result = await WebCollector(client).fetch(source)
    await client.aclose()

    outcome = await RuleExtractor().extract(result.messages[0].text)
    assert [item.title for item in outcome.items] == ["剧集甲", "剧集乙"]
    assert [[link.share_id for link in item.links] for item in outcome.items] == [["one"], ["two"]]
