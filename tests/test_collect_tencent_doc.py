from __future__ import annotations

import httpx
import pytest

from funflix.base.enums import SourceType
from funflix.models import Source
from funflix.services.collect.tencent_doc import (
    TencentDocCollector,
    TencentDocError,
    extract_plain_text,
    split_blocks,
)

DOC_ID = "DR2xUcFdrSVhJTkZu"


def build_payload(text: str, *, rev: int = 1, chunked: bool = False, title: str = "测试文档"):
    return {
        "bodyData": {"initialTitle": title},
        "clientVars": {
            "collab_client_vars": {
                "rev": rev,
                "isChunked": chunked,
                "initialAttributedText": {
                    "text": [
                        {
                            "commands": [
                                {
                                    "type": "DocKeyframe",
                                    "mutations": [
                                        {"ty": "mp", "bi": 0},
                                        {"ty": "is", "bi": 0, "s": text},
                                    ],
                                }
                            ]
                        }
                    ]
                },
            }
        },
    }


def hyperlink(url: str, label: str) -> str:
    """构造一个 RTF 风格的超链接字段码。"""
    return f"\x13HYPERLINK {url} docLink \\tdft \\tdfe 0\x14{label}\x15"


class TestExtractPlainText:
    def test_keeps_url_not_only_display_text(self) -> None:
        """显示文本是展示层，常是「点击查看」；URL 必须保留。"""
        text = extract_plain_text(
            build_payload("剧名A " + hyperlink("https://pan.quark.cn/s/a1", "点击查看"))
        )
        assert "https://pan.quark.cn/s/a1" in text

    def test_keeps_display_text_too(self) -> None:
        """显示文本常含剧名，不能连带丢掉。"""
        text = extract_plain_text(build_payload(hyperlink("https://pan.quark.cn/s/a1", "剧集甲")))
        assert "剧集甲" in text and "https://pan.quark.cn/s/a1" in text

    def test_carriage_return_becomes_newline(self) -> None:
        assert extract_plain_text(build_payload("第一段\r第二段")) == "第一段\n第二段"

    def test_strips_stray_control_characters(self) -> None:
        """残留控制符会污染 content_hash，导致同样的内容算出不同指纹。"""
        text = extract_plain_text(build_payload("正文\x08\x0f\x1e内容"))
        assert text == "正文内容"

    def test_concatenates_multiple_insert_mutations(self) -> None:
        """不能只取第一条 —— 别的文档可能把正文拆成多条 is。"""
        payload = build_payload("")
        muts = payload["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0][
            "commands"
        ][0]["mutations"]
        muts.clear()
        muts.extend([{"ty": "is", "s": "前半"}, {"ty": "mp"}, {"ty": "is", "s": "后半"}])
        assert extract_plain_text(payload) == "前半后半"

    def test_missing_commands_raises(self) -> None:
        with pytest.raises(TencentDocError, match="commands"):
            extract_plain_text({"clientVars": {}})

    def test_no_text_content_raises(self) -> None:
        payload = build_payload("")
        payload["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0]["commands"][
            0
        ]["mutations"] = [{"ty": "mp"}]
        with pytest.raises(TencentDocError, match="没有任何文本"):
            extract_plain_text(payload)


class TestSplitBlocks:
    def test_splits_on_blank_lines(self) -> None:
        assert split_blocks("剧集甲\n链接1\n\n剧集乙\n链接2") == ["剧集甲\n链接1", "剧集乙\n链接2"]

    def test_collapses_repeated_blank_lines(self) -> None:
        assert len(split_blocks("甲\n\n\n\n乙")) == 2

    def test_long_block_without_blank_lines_is_cut(self) -> None:
        text = "\n".join(f"第{i}行内容" for i in range(500))
        blocks = split_blocks(text, max_chars=200)
        assert len(blocks) > 1
        assert all(len(b) <= 200 for b in blocks)

    def test_cut_prefers_line_boundary(self) -> None:
        """硬切也要切在换行处，别把一行劈成两半。"""
        text = "\n".join(["x" * 50] * 20)
        assert all(not b.startswith("x" * 50 + "x") for b in split_blocks(text, max_chars=120))

    def test_empty_text_yields_nothing(self) -> None:
        assert split_blocks("") == []


def _collector(payload: dict) -> TencentDocCollector:
    return TencentDocCollector(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        )
    )


def _source(extra: dict | None = None) -> Source:
    return Source(
        id=1,
        source_type=SourceType.TENCENT_DOC,
        url=f"https://docs.qq.com/doc/{DOC_ID}",
        identifier=DOC_ID,
        extra=extra or {},
    )


class TestNormalizeIdentifier:
    def test_extracts_doc_id(self) -> None:
        assert (
            TencentDocCollector.normalize_identifier(f"https://docs.qq.com/doc/{DOC_ID}?dver=")
            == DOC_ID
        )

    def test_rejects_smartsheet_url(self) -> None:
        """表格由另一个采集器负责，两者结构完全不同。"""
        assert (
            TencentDocCollector.normalize_identifier("https://docs.qq.com/smartsheet/DT0xZd3")
            is None
        )


@pytest.mark.asyncio
class TestFetch:
    async def test_produces_one_message_per_block(self) -> None:
        payload = build_payload("剧集甲\r链接1\r\r剧集乙\r链接2", rev=7)
        result = await _collector(payload).fetch(_source())

        assert [m.text for m in result.messages] == ["剧集甲\n链接1", "剧集乙\n链接2"]
        assert result.title == "测试文档"
        assert result.state == {"tencent_doc_rev": 7}

    async def test_unchanged_rev_skips_entire_document(self) -> None:
        """一次就是全量（实测 26MB / 1.6 万段），版本没变就别重算所有 hash。"""
        payload = build_payload("剧集甲\r链接1", rev=7)
        result = await _collector(payload).fetch(_source(extra={"tencent_doc_rev": 7}))
        assert result.messages == []

    async def test_changed_rev_refetches(self) -> None:
        payload = build_payload("剧集甲\r链接1", rev=8)
        result = await _collector(payload).fetch(_source(extra={"tencent_doc_rev": 7}))
        assert len(result.messages) == 1

    async def test_chunked_document_raises_instead_of_losing_content(self) -> None:
        """分块返回时只发一次请求会拿到半份内容。

        继续处理并推进版本号 = 静默丢数据，是最难发现的一类 bug。
        """
        payload = build_payload("剧集甲", chunked=True)
        with pytest.raises(TencentDocError, match="isChunked"):
            await _collector(payload).fetch(_source())

    async def test_backfill_is_a_noop(self) -> None:
        result = await _collector(build_payload("甲")).backfill(_source())
        assert result.messages == []
        assert result.backfill_done is True
