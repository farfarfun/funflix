from __future__ import annotations

import base64
import json
import zlib

import httpx
import pytest

from funflix.base.enums import SourceType
from funflix.models import Source
from funflix.services.collect.registry import detect_source, supported_source_types
from funflix.services.collect.tencent_sheet import (
    TencentSheetCollector,
    TencentSheetError,
    cell_to_text,
    parse_page_context,
    parse_sheet_chunk,
    parse_sheet_ids,
    render_row,
)

DOC_ID = "DT0xZd3FMRHFKeXVT"


def _encode(ops: list) -> str:
    """按真实接口的三层编码构造 payload：JSON → zlib → base64url。"""
    raw = zlib.compress(json.dumps(ops).encode("utf-8"))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def build_payload(
    *,
    sheet_id: str = "tiAAAA",
    ver: int = 100,
    total_row: int = 2,
    sheet_ids: tuple[str, ...] = ("tiAAAA", "tiBBBB"),
    rows: dict | None = None,
    columns: dict | None = None,
) -> dict:
    columns = columns or {"fTitle": "剧名", "fQuark": "夸克", "fYear": "年份"}
    rows = rows if rows is not None else {}

    ops = [
        [
            {
                "t": 3005,
                "c": {
                    "k1": sheet_id,
                    "k3": {"k3": {fid: {"k30": name, "k31": 1} for fid, name in columns.items()}},
                },
            },
            {"t": 3028, "c": {"k2": {"k1": rows}}},
        ]
    ]
    permission = base64.b64encode(
        json.dumps(
            [{"name": "全员权限", "priv": {"items": [{"sheet_id": s} for s in sheet_ids]}}]
        ).encode()
    ).decode()

    return {
        "clientVars": {
            "title": "测试文档",
            "collab_client_vars": {
                "initialAttributedText": {"text": [{"smartsheet": _encode(ops)}]},
                "smartsheetConfig": json.dumps(
                    {
                        "pageCtx": json.dumps(
                            {"ver": ver, "sheet_id": sheet_id, "total_row": total_row}
                        )
                    }
                ),
                "smartsheetPermission": permission,
            },
        }
    }


def text_row(title: str, url: str, year: str = "2026") -> dict:
    return {
        "k1": {
            "fTitle": {"k1": {"k1": title}, "k30": "x", "k31": 1},
            "fQuark": {"k8": [{"k2": "点击查看", "k3": url}], "k30": "x", "k31": 8},
            "fYear": {"k17": {"k1": year}, "k30": "x", "k31": 17},
        }
    }


class TestNormalizeIdentifier:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://docs.qq.com/smartsheet/{DOC_ID}?tab=tiCCQP",
            f"https://docs.qq.com/smartsheet/{DOC_ID}",
            f"https://docs.qq.com/sheet/{DOC_ID}",
        ],
    )
    def test_extracts_doc_id_ignoring_tab(self, url: str) -> None:
        """一个文档 = 一个 Source：tab 参数不参与身份判定。"""
        assert TencentSheetCollector.normalize_identifier(url) == DOC_ID

    def test_rejects_unrelated_url(self) -> None:
        assert TencentSheetCollector.normalize_identifier("https://example.com/x") is None

    def test_registry_detects_tencent_before_telegram(self) -> None:
        """Telegram 的兜底模式能匹配裸标识串，顺序错了会抢走腾讯的 URL。"""
        assert detect_source(f"https://docs.qq.com/smartsheet/{DOC_ID}") == (
            SourceType.TENCENT_DOCS,
            DOC_ID,
        )

    def test_telegram_still_detected(self) -> None:
        assert detect_source("https://t.me/s/SomeChannel")[0] is SourceType.TELEGRAM

    def test_all_source_types_registered(self) -> None:
        assert set(supported_source_types()) == {
            SourceType.TELEGRAM,
            SourceType.TENCENT_DOCS,
            SourceType.TENCENT_DOC,
            SourceType.RSS,
            SourceType.API,
        }

    def test_doc_url_does_not_route_to_sheet_collector(self) -> None:
        """`/doc/` 是文本文档，结构与编码和表格完全不同。

        表格采集器的模式若把 `/doc/` 也匹配上，会抢走文本文档的 URL，
        然后在三层解码的第一步就炸。
        """
        assert (
            TencentSheetCollector.normalize_identifier("https://docs.qq.com/doc/DR2xUcFdrSVhJTkZu")
            is None
        )
        assert detect_source("https://docs.qq.com/doc/DR2xUcFdrSVhJTkZu") == (
            SourceType.TENCENT_DOC,
            "DR2xUcFdrSVhJTkZu",
        )


class TestDecoding:
    def test_parses_sheet_ids_from_permission_blob(self) -> None:
        payload = build_payload(sheet_ids=("tiAAAA", "tiBBBB", "tiCCCC"))
        assert parse_sheet_ids(payload) == ["tiAAAA", "tiBBBB", "tiCCCC"]

    def test_parses_page_context(self) -> None:
        ctx = parse_page_context(build_payload(ver=52844, total_row=39466))
        assert (ctx["ver"], ctx["total_row"]) == (52844, 39466)

    def test_parses_columns_and_rows(self) -> None:
        from funflix.services.collect.tencent_sheet import _decode_smartsheet

        payload = build_payload(rows={"rAAA": text_row("剧集甲", "https://pan.quark.cn/s/a1")})
        columns, rows = parse_sheet_chunk(_decode_smartsheet(payload))

        assert columns["fTitle"] == "剧名"
        assert list(rows) == ["rAAA"]

    def test_broken_structure_raises_instead_of_returning_empty(self) -> None:
        """三层解码任一层变了都要炸 —— 静默返回空列表会被误读成"没有更新"。"""
        with pytest.raises(TencentSheetError, match="smartsheet"):
            parse_sheet_ids({"clientVars": {}})

    def test_corrupt_payload_raises(self) -> None:
        from funflix.services.collect.tencent_sheet import _decode_smartsheet

        broken = build_payload()
        broken["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0][
            "smartsheet"
        ] = "not-valid-base64-zlib"
        with pytest.raises(TencentSheetError):
            _decode_smartsheet(broken)


class TestCellRendering:
    def test_link_cell_yields_href_not_display_text(self) -> None:
        """显示文本是展示层，长链接会被截断，没有等于 href 的契约。"""
        cell = {"k8": [{"k2": "点击查看", "k3": "https://pan.quark.cn/s/abc123"}]}
        assert cell_to_text(cell) == "https://pan.quark.cn/s/abc123"

    def test_text_cell_yields_value(self) -> None:
        assert cell_to_text({"k1": {"k1": "剧集甲"}}) == "剧集甲"

    def test_metadata_keys_are_skipped(self) -> None:
        cell = {"k1": {"k1": "剧集甲"}, "k30": "内部元数据", "k31": 1, "k32": 99}
        assert cell_to_text(cell) == "剧集甲"

    def test_empty_cell_yields_empty_string(self) -> None:
        assert cell_to_text({"k1": {}}) == ""

    def test_renders_row_with_column_labels(self) -> None:
        rendered = render_row(
            {"fTitle": "剧名", "fQuark": "夸克"},
            {
                "fTitle": {"k1": {"k1": "剧集甲"}},
                "fQuark": {"k8": [{"k2": "查看", "k3": "https://pan.quark.cn/s/a1"}]},
            },
        )
        assert rendered == "剧名：剧集甲\n夸克：https://pan.quark.cn/s/a1"

    def test_empty_cells_are_omitted(self) -> None:
        rendered = render_row(
            {"fTitle": "剧名", "fNote": "备注"},
            {"fTitle": {"k1": {"k1": "剧集甲"}}, "fNote": {"k1": {}}},
        )
        assert "备注" not in rendered


def _collector(pages: dict) -> TencentSheetCollector:
    """按 (tab, startrow) 返回预置 payload。"""
    requests: list[tuple[str | None, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tab = request.url.params.get("tab")
        start = int(request.url.params.get("startrow", 0))
        requests.append((tab, start))
        return httpx.Response(200, json=pages.get((tab, start), build_payload(rows={})))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = TencentSheetCollector(client=client, chunk_delay=0)
    collector.requests = requests  # type: ignore[attr-defined]
    return collector


def _source(extra: dict | None = None, max_pages: int = 20) -> Source:
    return Source(
        id=1,
        source_type=SourceType.TENCENT_DOCS,
        url=f"https://docs.qq.com/smartsheet/{DOC_ID}",
        identifier=DOC_ID,
        max_pages_per_fetch=max_pages,
        extra=extra or {},
    )


@pytest.mark.asyncio
class TestFetch:
    async def test_collects_rows_from_every_sheet(self) -> None:
        collector = _collector(
            {
                (None, 0): build_payload(sheet_ids=("tiAAAA", "tiBBBB")),
                ("tiAAAA", 0): build_payload(
                    sheet_id="tiAAAA",
                    ver=1,
                    total_row=1,
                    rows={"rA": text_row("剧集甲", "https://pan.quark.cn/s/a1")},
                ),
                ("tiBBBB", 0): build_payload(
                    sheet_id="tiBBBB",
                    ver=2,
                    total_row=1,
                    rows={"rB": text_row("剧集乙", "https://pan.quark.cn/s/b1")},
                ),
            }
        )
        result = await collector.fetch(_source())

        assert [m.message_id for m in result.messages] == ["tiAAAA:rA", "tiBBBB:rB"]
        assert "剧名：剧集甲" in result.messages[0].text
        assert "https://pan.quark.cn/s/a1" in result.messages[0].text

    async def test_reports_versions_as_state(self) -> None:
        collector = _collector(
            {
                (None, 0): build_payload(sheet_ids=("tiAAAA",)),
                ("tiAAAA", 0): build_payload(
                    sheet_id="tiAAAA",
                    ver=52844,
                    total_row=1,
                    rows={"rA": text_row("甲", "https://pan.quark.cn/s/a1")},
                ),
            }
        )
        result = await collector.fetch(_source())
        assert result.state["tencent_sheet_versions"] == {"tiAAAA": 52844}
        # 同时记下扫到第几行、总共多少行，backfill 才有续扫的起点
        assert "tencent_sheet_offsets" in result.state
        assert result.state["tencent_sheet_totals"] == {"tiAAAA": 1}

    async def test_unchanged_version_skips_the_sheet(self) -> None:
        """版本没变就整个 sheet 跳过 —— 一次请求判完，不翻几百个分片。"""
        collector = _collector(
            {
                (None, 0): build_payload(sheet_ids=("tiAAAA",)),
                ("tiAAAA", 0): build_payload(
                    sheet_id="tiAAAA",
                    ver=100,
                    total_row=999,
                    rows={"rA": text_row("甲", "https://pan.quark.cn/s/a1")},
                ),
            }
        )
        result = await collector.fetch(_source(extra={"tencent_sheet_versions": {"tiAAAA": 100}}))

        assert result.messages == []
        # 只发了「清单」和「该 sheet 首片」两个请求，没继续翻页
        assert collector.requests == [(None, 0), ("tiAAAA", 0)]  # type: ignore[attr-defined]

    async def test_changed_version_refetches(self) -> None:
        collector = _collector(
            {
                (None, 0): build_payload(sheet_ids=("tiAAAA",)),
                ("tiAAAA", 0): build_payload(
                    sheet_id="tiAAAA",
                    ver=101,
                    total_row=1,
                    rows={"rA": text_row("甲", "https://pan.quark.cn/s/a1")},
                ),
            }
        )
        result = await collector.fetch(_source(extra={"tencent_sheet_versions": {"tiAAAA": 100}}))
        assert len(result.messages) == 1

    async def test_fetch_initializes_every_sheet_before_backfill(self) -> None:
        collector = _collector(
            {
                (None, 0): build_payload(sheet_ids=("tiAAAA", "tiBBBB")),
                ("tiAAAA", 0): build_payload(
                    sheet_id="tiAAAA",
                    ver=1,
                    total_row=39_724,
                    rows={"rA": text_row("剧A", "https://pan.quark.cn/s/a")},
                ),
                ("tiBBBB", 0): build_payload(
                    sheet_id="tiBBBB",
                    ver=2,
                    total_row=22_161,
                    rows={"rB": text_row("剧B", "https://pan.quark.cn/s/b")},
                ),
            }
        )
        result = await collector.fetch(_source(max_pages=1))

        assert collector.requests == [(None, 0), ("tiAAAA", 0), ("tiBBBB", 0)]  # type: ignore[attr-defined]
        assert result.state["tencent_sheet_totals"] == {
            "tiAAAA": 39_724,
            "tiBBBB": 22_161,
        }
        assert result.backfill_pending is True

    async def test_backfill_returns_one_checkpointed_block(self) -> None:
        collector = _collector(
            {
                ("tiAAAA", 0): build_payload(sheet_id="tiAAAA", total_row=180),
                ("tiAAAA", 60): build_payload(
                    sheet_id="tiAAAA",
                    ver=7,
                    total_row=180,
                    rows={"r60": text_row("剧60", "https://pan.quark.cn/s/x60")},
                ),
            }
        )
        source = _source(
            extra={
                "tencent_sheet_offsets": {"tiAAAA": 60},
                "tencent_sheet_totals": {"tiAAAA": 180},
            },
            max_pages=1,
        )

        result = await collector.backfill(source)

        assert [m.message_id for m in result.messages] == ["tiAAAA:r60"]
        assert result.state["tencent_sheet_offsets"] == {"tiAAAA": 120}
        assert result.backfill_done is False

    async def test_backfill_records_version_after_last_block(self) -> None:
        collector = _collector(
            {
                ("tiAAAA", 0): build_payload(sheet_id="tiAAAA", total_row=180),
                ("tiAAAA", 120): build_payload(
                    sheet_id="tiAAAA",
                    ver=7,
                    total_row=180,
                    rows={"r120": text_row("剧120", "https://pan.quark.cn/s/x120")},
                ),
            }
        )
        source = _source(
            extra={
                "tencent_sheet_offsets": {"tiAAAA": 120},
                "tencent_sheet_totals": {"tiAAAA": 180},
            },
            max_pages=1,
        )

        result = await collector.backfill(source)

        assert result.state["tencent_sheet_versions"] == {"tiAAAA": 7}
        assert result.backfill_done is True

    async def test_changed_completed_version_keeps_finished_offset(self) -> None:
        collector = _collector(
            {
                (None, 0): build_payload(sheet_ids=("tiAAAA",)),
                ("tiAAAA", 0): build_payload(
                    sheet_id="tiAAAA",
                    ver=101,
                    total_row=180,
                    rows={"r0": text_row("新剧", "https://pan.quark.cn/s/new")},
                ),
            }
        )
        source = _source(
            extra={
                "tencent_sheet_versions": {"tiAAAA": 100},
                "tencent_sheet_offsets": {"tiAAAA": 180},
                "tencent_sheet_totals": {"tiAAAA": 180},
            }
        )

        result = await collector.fetch(source)

        assert result.state["tencent_sheet_versions"] == {"tiAAAA": 101}
        assert result.state["tencent_sheet_offsets"] == {"tiAAAA": 180}
        assert result.backfill_pending is False

    async def test_version_not_advanced_when_truncated(self) -> None:
        """没取完就推进版本号，下轮会误以为已同步、永久丢掉剩余的行。"""
        collector = _collector(
            {
                (None, 0): build_payload(sheet_ids=("tiAAAA",)),
                ("tiAAAA", 0): build_payload(
                    sheet_id="tiAAAA",
                    ver=7,
                    total_row=10_000,
                    rows={
                        f"r{i}": text_row(f"剧{i}", f"https://pan.quark.cn/s/x{i}")
                        for i in range(61)
                    },
                ),
            }
        )
        result = await collector.fetch(_source(max_pages=3))

        assert result.truncated is True
        # 版本号没有推进 —— 推进了下轮就会跳过这个 sheet，剩下的行永久丢失
        assert "tencent_sheet_versions" not in result.state
