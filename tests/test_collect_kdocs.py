from __future__ import annotations

import json

import httpx
import pytest

from funflix.base.enums import SourceType
from funflix.models import Source
from funflix.services.collect.kdocs import KDocsCollector, cell_to_text, render_record
from funflix.services.collect.registry import detect_source

LINK_ID = "ck4zloFnPg8f"


def _sheet(total: int) -> dict:
    return {
        "id": 1,
        "name": "影视",
        "recordsCount": total,
        "fields": [
            {"name": "剧名", "type": "MultiLineText"},
            {"name": "夸克", "type": "Url"},
            {"name": "海报", "type": "Attachment"},
        ],
    }


def _record(record_id: str, title: str, url: str) -> dict:
    return {
        "id": record_id,
        "fields": {
            "剧名": title,
            "夸克": [{"address": url, "displayText": "查看"}],
            "海报": [{"address": "https://img.example/poster.jpg"}],
        },
    }


def _source(extra: dict | None = None, max_pages: int = 1) -> Source:
    return Source(
        source_type=SourceType.KDOCS,
        url=f"http://kdocs.cn/l/{LINK_ID}?R=L1MvMQ==",
        identifier=LINK_ID,
        max_pages_per_fetch=max_pages,
        extra=extra or {},
    )


def _collector(total: int, pages: dict[str | None, dict]) -> tuple[KDocsCollector, list]:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body["command"] == "http.db.listSheets":
            return httpx.Response(200, json={"result": "ok", "detail": {"sheets": [_sheet(total)]}})
        offset = body["param"].get("offset")
        return httpx.Response(200, json={"result": "ok", "detail": pages[offset]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KDocsCollector(client=client, page_delay=0), requests


def test_normalizes_public_link_and_uses_sheet_extractor() -> None:
    assert detect_source(f"http://kdocs.cn/l/{LINK_ID}?R=x") == (SourceType.KDOCS, LINK_ID)


def test_renders_url_address_and_ignores_attachment() -> None:
    assert cell_to_text([{"address": "https://pan.quark.cn/s/abc", "displayText": "查看"}]) == (
        "https://pan.quark.cn/s/abc"
    )
    assert "海报" not in render_record(_record("A", "剧集甲", "https://x")["fields"], {"海报"})


@pytest.mark.asyncio
async def test_fetch_and_backfill_follow_opaque_offsets() -> None:
    collector, requests = _collector(
        3,
        {
            None: {
                "records": [_record("A", "剧集甲", "https://pan.quark.cn/s/a")],
                "offset": "B",
            },
            "B": {
                "records": [_record("B", "剧集乙", "https://pan.quark.cn/s/b")],
                "offset": "C",
            },
            "C": {"records": [_record("C", "剧集丙", "https://pan.quark.cn/s/c")]},
        },
    )
    source = _source()

    first = await collector.fetch(source)
    assert [m.message_id for m in first.messages] == ["1:A"]
    assert "海报" not in first.messages[0].text
    assert first.state["kdocs_offsets"] == {"1": "B"}

    source.extra = first.state
    second = await collector.backfill(source)
    assert [m.message_id for m in second.messages] == ["1:B"]
    assert second.state["kdocs_offsets"] == {"1": "C"}

    source.extra = second.state
    final = await collector.backfill(source)
    assert [m.message_id for m in final.messages] == ["1:C"]
    assert final.backfill_done is True
    assert final.state["kdocs_tail_offsets"] == {"1": "C"}
    assert [r["param"].get("offset") for r in requests if r["command"].endswith("listRecords")] == [
        None,
        "B",
        "C",
    ]


@pytest.mark.asyncio
async def test_completed_sheet_resumes_from_tail_only_after_growth() -> None:
    collector, requests = _collector(
        3,
        {
            "B": {
                "records": [
                    _record("B", "旧记录", "https://pan.quark.cn/s/b"),
                    _record("C", "新增记录", "https://pan.quark.cn/s/c"),
                ]
            }
        },
    )
    source = _source(
        {
            "kdocs_offsets": {},
            "kdocs_tail_offsets": {"1": "B"},
            "kdocs_totals": {"1": 2},
            "kdocs_completed_sheets": ["1"],
        }
    )

    result = await collector.fetch(source)
    assert [m.message_id for m in result.messages] == ["1:B", "1:C"]
    record_request = next(r for r in requests if r["command"] == "http.db.listRecords")
    assert record_request["param"]["offset"] == "B"
    assert result.backfill_pending is False
