from __future__ import annotations

import pytest
from stubs import StubLLM, item, payload

from funflix.base.enums import MediaType, Provider, Quality
from funflix.services.extract.llm.extractor import (
    LLMExtractor,
    format_link_lines,
    parse_payload,
)
from funflix.services.text.linkscan import scan_links

TEXT_TWO_WORKS = (
    "名称：剧集甲\n"
    "夸克：https://pan.quark.cn/s/aaa111\n"
    "阿里：https://www.alipan.com/s/aaa222\n"
    "名称：剧集乙\n"
    "夸克：https://pan.quark.cn/s/bbb111\n"
)


def _links(text: str = TEXT_TWO_WORKS):
    return scan_links(text)


class TestFormatLinkLines:
    def test_numbers_links_from_zero(self) -> None:
        lines = format_link_lines(_links())
        assert lines[0].startswith("[0] https://pan.quark.cn/s/aaa111")
        assert lines[2].startswith("[2] https://pan.quark.cn/s/bbb111")

    def test_includes_passcode_when_present(self) -> None:
        lines = format_link_lines(scan_links("https://pan.baidu.com/s/1abcdef 提取码：8k2m"))
        assert "8k2m" in lines[0]


class TestAttribution:
    def test_multiple_works_each_with_multiple_links(self) -> None:
        links = _links()
        outcome = parse_payload(
            payload(item("剧集甲", indexes=[0, 1]), item("剧集乙", indexes=[2])), links
        )

        assert [i.title for i in outcome.items] == ["剧集甲", "剧集乙"]
        assert [[link.share_id for link in i.links] for i in outcome.items] == [
            ["aaa111", "aaa222"],
            ["bbb111"],
        ]
        assert outcome.unattributed_links == []

    def test_unreferenced_links_are_reported_not_dropped(self) -> None:
        links = _links()
        outcome = parse_payload(payload(item("剧集甲", indexes=[0])), links)

        assert [link.share_id for link in outcome.unattributed_links] == ["aaa222", "bbb111"]
        assert outcome.stats["links_unattributed"] == 2

    def test_every_link_is_either_attributed_or_unattributed(self) -> None:
        links = _links()
        outcome = parse_payload(payload(item("剧集甲", indexes=[1])), links)
        total = sum(len(i.links) for i in outcome.items) + len(outcome.unattributed_links)
        assert total == len(links)


class TestHallucinationGuards:
    def test_out_of_range_index_is_dropped_and_counted(self) -> None:
        """序号越界 = 模型编了一条不存在的链接。"""
        links = _links()
        outcome = parse_payload(payload(item("剧集甲", indexes=[0, 99])), links)

        assert [link.share_id for link in outcome.items[0].links] == ["aaa111"]
        assert outcome.stats["invalid_link_index"] == 1

    def test_negative_index_is_dropped(self) -> None:
        outcome = parse_payload(payload(item("剧集甲", indexes=[-1])), _links())
        assert outcome.items[0].links == []
        assert outcome.stats["invalid_link_index"] == 1

    def test_non_integer_index_is_dropped(self) -> None:
        outcome = parse_payload(payload(item("剧集甲", indexes=["https://evil"])), _links())
        assert outcome.items[0].links == []
        assert outcome.stats["invalid_link_index"] == 1

    def test_same_link_claimed_twice_goes_to_first_work_only(self) -> None:
        links = _links()
        outcome = parse_payload(
            payload(item("剧集甲", indexes=[0]), item("剧集乙", indexes=[0, 2])), links
        )

        assert [link.share_id for link in outcome.items[0].links] == ["aaa111"]
        assert [link.share_id for link in outcome.items[1].links] == ["bbb111"]
        assert outcome.stats["duplicate_link_assignment"] == 1

    def test_empty_title_item_is_dropped(self) -> None:
        outcome = parse_payload(payload(item("   ", indexes=[0])), _links())
        assert outcome.items == []
        assert outcome.stats["empty_title_dropped"] == 1

    def test_malformed_payload_yields_no_items(self) -> None:
        outcome = parse_payload({"is_catalog": False, "items": "不是数组"}, _links())
        assert outcome.items == []
        assert outcome.stats["links_unattributed"] == 3


class TestFieldCoercion:
    def test_model_title_is_re_cleaned_deterministically(self) -> None:
        """模型剥噪声并不稳定，返回的标题再过一遍 clean_title。"""
        outcome = parse_payload(payload(item("剧集甲 1080p 中字", indexes=[])), _links())
        assert outcome.items[0].title == "剧集甲"

    @pytest.mark.parametrize("year", [1800, 3000, "2024", None, True])
    def test_implausible_year_becomes_none(self, year: object) -> None:
        outcome = parse_payload(payload(item("剧集甲", indexes=[], year=year)), _links())
        assert outcome.items[0].year is None

    def test_valid_year_is_kept(self) -> None:
        outcome = parse_payload(payload(item("剧集甲", indexes=[], year=2026)), _links())
        assert outcome.items[0].year == 2026

    def test_unknown_enum_value_falls_back_to_default(self) -> None:
        outcome = parse_payload(
            payload(item("剧集甲", indexes=[], media_type="超能力片", quality="8k超清")),
            _links(),
        )
        assert outcome.items[0].media_type is MediaType.UNKNOWN
        assert outcome.items[0].quality is Quality.UNKNOWN

    def test_valid_enum_values_are_parsed(self) -> None:
        outcome = parse_payload(
            payload(item("剧集甲", indexes=[], media_type="anime", quality="4k")), _links()
        )
        assert outcome.items[0].media_type is MediaType.ANIME
        assert outcome.items[0].quality is Quality.UHD_4K


class TestCatalog:
    def test_catalog_flag_is_propagated(self) -> None:
        outcome = parse_payload(payload(is_catalog=True), _links())
        assert outcome.is_catalog is True
        assert outcome.items == []
        # 目录帖的链接仍然要保留
        assert len(outcome.unattributed_links) == 3


@pytest.mark.asyncio
class TestLLMExtractor:
    async def test_passes_scanned_links_to_the_model(self) -> None:
        client = StubLLM(payload(item("剧集甲", indexes=[0])))
        await LLMExtractor(client).extract(TEXT_TWO_WORKS)

        assert "[0] https://pan.quark.cn/s/aaa111" in client.last_user
        assert "[2] https://pan.quark.cn/s/bbb111" in client.last_user

    async def test_records_usage_metrics(self) -> None:
        client = StubLLM(payload(item("剧集甲", indexes=[0])))
        outcome = await LLMExtractor(client).extract(TEXT_TWO_WORKS)

        assert outcome.extractor_name == "stub-model"
        assert (outcome.input_tokens, outcome.output_tokens) == (100, 50)
        assert outcome.latency_ms == 42

    async def test_text_without_links_still_extracts_titles(self) -> None:
        client = StubLLM(payload(item("剧集甲", indexes=[])))
        outcome = await LLMExtractor(client).extract("名称：剧集甲\n暂无资源")

        assert outcome.items[0].title == "剧集甲"
        assert outcome.all_links == []

    async def test_provider_is_shown_to_the_model(self) -> None:
        client = StubLLM()
        await LLMExtractor(client).extract("https://pan.quark.cn/s/aaa111")
        assert Provider.QUARK.value in client.last_user

    async def test_extractor_identity_comes_from_model_name(self) -> None:
        """抽取器身份即缓存键：换模型必须换身份，否则会命中旧结果。"""
        extractor = LLMExtractor(StubLLM(model="model-a"))
        assert extractor.name == "model-a"

    async def test_rehydrate_does_not_call_the_model(self) -> None:
        client = StubLLM(payload(item("剧集甲", indexes=[0])))
        extractor = LLMExtractor(client)
        cached = (await extractor.extract(TEXT_TWO_WORKS)).raw_payload
        assert client.calls == 1

        outcome = extractor.rehydrate(cached, TEXT_TWO_WORKS)
        assert client.calls == 1  # 没有二次调用
        assert outcome.items[0].links[0].share_id == "aaa111"
