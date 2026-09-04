from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import uuid
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from funflix.base.enums import Provider, SourceType
from funflix.models import Source
from funflix.services.collect import yyets
from funflix.services.collect.registry import detect_source
from funflix.services.collect.yyets import YYeTsCollector
from funflix.services.extract.rule import RuleExtractor
from funflix.services.text.linkscan import scan_links


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


def _source(url: str, identifier: str, extra: dict | None = None) -> Source:
    return Source(
        id=uuid.UUID(int=1),
        source_type=SourceType.API,
        url=url,
        identifier=identifier,
        extra=extra or {},
    )


def test_detects_snapshot_and_comments_as_separate_sources() -> None:
    assert detect_source(yyets._SNAPSHOT_URL) == (
        SourceType.API,
        "yyets-snapshot-2021-08-22",
    )
    assert detect_source("https://yyets.click/api/comment/newest") == (
        SourceType.API,
        "yyets-comments",
    )
    assert detect_source("https://yyets.click/api/resource/latest") is None


@pytest.mark.asyncio
async def test_snapshot_reads_verified_sqlite_zip(tmp_path, monkeypatch) -> None:
    database = tmp_path / "yyets.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE yyets "
        "(id int, cnname text, enname text, aliasname text, views int, data text)"
    )
    connection.execute(
        "INSERT INTO yyets VALUES (?, ?, ?, ?, ?, ?)",
        (1, "剧集1", "Show 1", "", 0, json.dumps(_detail(1), ensure_ascii=False)),
    )
    connection.commit()
    connection.close()

    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("yyets.sqlite", database.read_bytes())
    payload = buffer.getvalue()
    monkeypatch.setattr(yyets, "_SNAPSHOT_SHA1", hashlib.sha1(payload).hexdigest())

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    )
    collector = YYeTsCollector(client=client)
    result = await collector.fetch(_source(yyets._SNAPSHOT_URL, "yyets-snapshot-2021-08-22"))
    await client.aclose()

    assert len(result.messages) == 1
    assert result.backfill_done is True
    assert result.state["yyets_snapshot_sha1"] == hashlib.sha1(payload).hexdigest()
    assert [link.provider for link in scan_links(result.messages[0].text)] == [
        Provider.ED2K,
        Provider.BAIDU,
    ]
    outcome = await RuleExtractor().extract(result.messages[0].text)
    assert outcome.items[0].episode_info == "第1季 第1集"
    assert outcome.items[0].size_bytes == 1024**3


@pytest.mark.asyncio
async def test_comments_track_recent_ids_and_backfill_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "count": 3,
                    "data": [
                        {
                            "id": "c1",
                            "date": "2026-09-02 15:43:53",
                            "resource_id": 42,
                            "cnname": "剧集甲",
                            "content": "https://pan.baidu.com/s/1share1 提取码：8k2m",
                        },
                        {"id": "c2", "resource_id": 233, "content": "没有资源链接"},
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "count": 3,
                "data": [
                    {
                        "id": "c3",
                        "resource_id": 233,
                        "cnname": "留言板",
                        "content": "【电影乙】\ned2k://|file|Movie.mkv|123|ABCDEF0123456789ABCDEF0123456789|/",
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = YYeTsCollector(client=client)
    source = _source("https://yyets.click/api/comment/newest", "yyets-comments")
    first = await collector.fetch(source)
    second = await collector.fetch(_source(source.url, source.identifier, first.state))
    backfill = await collector.backfill(_source(source.url, source.identifier, first.state))
    await client.aclose()

    assert [message.message_id for message in first.messages] == ["comment:c1"]
    assert second.messages == []
    assert set(first.state["yyets_comment_recent_ids"]) == {"comment:c1", "comment:c2"}
    assert first.messages[0].text.startswith("名称：剧集甲")
    assert scan_links(first.messages[0].text)[0].passcode == "8k2m"
    assert [message.message_id for message in backfill.messages] == ["comment:c3"]
    assert backfill.messages[0].text.startswith("【电影乙】")
    assert backfill.state["yyets_comment_backfill_page"] == 3
    assert backfill.backfill_done is True
