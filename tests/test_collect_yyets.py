from __future__ import annotations

import uuid

import httpx
import pytest

from funflix.base.enums import Provider, SourceType
from funflix.models import Source
from funflix.services.collect.registry import detect_source
from funflix.services.collect.yyets import YYeTsCollector
from funflix.services.text.linkscan import scan_links

LATEST = {
    "data": [
        {"resource_id": 1, "timestamp": "10"},
        {"resource_id": 2, "timestamp": "11"},
        {"resource_id": 1, "timestamp": "12"},
    ]
}


def _detail(resource_id: int) -> dict:
    return {
        "status": 1,
        "data": {
            "info": {
                "id": resource_id,
                "cnname": f"剧集{resource_id}",
                "enname": f"Show {resource_id}",
                "year": [2026],
            },
            "list": [
                {
                    "season_cn": "第1季",
                    "items": {
                        "WEB-DL": [
                            {
                                "episode": "1",
                                "name": f"Show.S01E0{resource_id}.mkv",
                                "size": "1GB",
                                "files": [
                                    {
                                        "way_cn": "电驴",
                                        "address": "ed2k://|file|Show.mkv|123|ABCDEF0123456789ABCDEF0123456789|/",
                                        "passwd": "",
                                    },
                                    {
                                        "way_cn": "百度云",
                                        "address": f"https://pan.baidu.com/s/1share{resource_id}",
                                        "passwd": "8k2m",
                                    },
                                ],
                            }
                        ]
                    },
                }
            ],
        },
    }


def _source(extra: dict | None = None) -> Source:
    return Source(
        id=uuid.UUID(int=1),
        source_type=SourceType.API,
        url="https://yyets.click/api/resource/latest",
        identifier="yyets.click",
        extra=extra or {},
    )


def test_detects_only_yyets_api() -> None:
    assert detect_source("https://yyets.click/api/resource/latest") == (
        SourceType.API,
        "yyets.click",
    )
    assert detect_source("https://example.com/api/resource/latest") is None


@pytest.mark.asyncio
async def test_fetches_details_and_tracks_versions() -> None:
    detail_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_requests
        if request.url.path == "/api/resource/latest":
            return httpx.Response(200, json=LATEST)
        detail_requests += 1
        return httpx.Response(200, json=_detail(int(request.url.params["id"])))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = YYeTsCollector(client=client)
    first = await collector.fetch(_source())
    second = await collector.fetch(_source(first.state))
    await client.aclose()

    assert [message.message_id for message in first.messages] == ["2:11", "1:12"]
    assert second.messages == []
    assert detail_requests == 2
    assert set(first.state["yyets_seen_versions"]) == {"1:12", "2:11"}
    links = scan_links(first.messages[0].text)
    assert [link.provider for link in links] == [Provider.ED2K, Provider.BAIDU]
    assert links[1].passcode == "8k2m"
