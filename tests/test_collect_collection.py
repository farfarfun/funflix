from __future__ import annotations

import json

import httpx
import pytest

from funflix.base.enums import Provider, SourceType
from funflix.models import Source
from funflix.services.collect.collection import CollectionCollector
from funflix.services.collect.registry import detect_source
from funflix.services.extract.rule import RuleExtractor


def _payload(discussion_id: int, title: str, html: str) -> dict:
    return {
        "data": {
            "type": "discussions",
            "id": str(discussion_id),
            "attributes": {"title": title, "createdAt": "2026-09-04T16:41:06+00:00"},
        },
        "included": [
            {
                "type": "posts",
                "id": str(discussion_id + 1000),
                "attributes": {"contentHtml": html},
            }
        ],
    }


def _wrapped(payload: dict) -> str:
    return "Title:\n\nMarkdown Content:\n" + json.dumps(payload)


def _source(**values) -> Source:
    return Source(
        id=1,
        source_type=SourceType.FORUM,
        url="https://forum.example/d/48861",
        identifier="48861",
        max_pages_per_fetch=1,
        extra={},
        **values,
    )


@pytest.mark.asyncio
async def test_collects_daily_pages_and_backfills() -> None:
    index = _payload(
        48861,
        "电影云集日更合集",
        "".join(
            f'<p><a href="https://forum.example/d/{value}">每日</a></p>'
            for value in (100, 200, 300)
        ),
    )

    def daily(value: int) -> dict:
        return _payload(
            value,
            f"2026年9月{value // 100}日更新",
            '<p>夸克网盘<br><a href="https://pan.quark.cn/s/shared">链接</a></p>'
            '<p>百度网盘<br><a href="https://pan.baidu.com/s/1shared?pwd=abcd">链接</a></p>'
            "<p>01. 剧集甲（75集）</p><p>02. 剧集乙（60集）</p>",
        )

    payloads = {48861: index, 100: daily(100), 200: daily(200), 300: daily(300)}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "forum.example" in str(request.url)
        return httpx.Response(200, text=_wrapped(payloads[int(request.url.path.rsplit("/", 1)[1])]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = CollectionCollector(client)
    source = _source()

    current = await collector.fetch(source)
    source.extra = current.state
    source.backfill_cursor_id = current.messages[0].message_id
    older = await collector.backfill(source)
    source.backfill_cursor_id = older.backfill_cursor
    oldest = await collector.backfill(source)
    await client.aclose()

    assert detect_source(source.url) == (SourceType.FORUM, "forum.example:48861")
    assert [message.message_id for message in current.messages] == ["300"]
    assert [message.message_id for message in older.messages] == ["200"]
    assert oldest.backfill_done is True

    outcome = await RuleExtractor().extract(current.messages[0].text)
    assert [item.title for item in outcome.items] == ["剧集甲", "剧集乙"]
    assert [[link.provider for link in item.links] for item in outcome.items] == [
        [Provider.QUARK, Provider.BAIDU],
        [Provider.QUARK, Provider.BAIDU],
    ]
