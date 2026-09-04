from __future__ import annotations

import pytest

from funflix.base.enums import MediaType
from funflix.services.extract.registry import get_extractor, supported_extractors
from funflix.services.extract.rule import RuleExtractor


class TestRegistry:
    def test_lists_all_implementations(self) -> None:
        assert supported_extractors() == ["llm", "rule", "sheet"]

    def test_builds_rule_extractor(self) -> None:
        assert isinstance(get_extractor("rule"), RuleExtractor)

    def test_unknown_kind_raises_with_options(self) -> None:
        with pytest.raises(ValueError, match="rule"):
            get_extractor("不存在的抽取器")

    def test_rule_extractor_needs_no_credentials(self) -> None:
        """规则抽取器不该碰 funsecret —— 否则"离线降级"就不成立。"""
        extractor = get_extractor("rule")
        assert extractor.name == "rule"


@pytest.mark.asyncio
class TestRuleExtractor:
    async def test_extracts_single_work_with_link(self) -> None:
        outcome = await RuleExtractor().extract(
            "名称：测试剧集 (2026) 全40集\n夸克：https://pan.quark.cn/s/aaa111"
        )

        assert len(outcome.items) == 1
        item = outcome.items[0]
        assert item.title == "测试剧集"
        assert item.year == 2026
        assert item.media_type is MediaType.TV
        assert [link.share_id for link in item.links] == ["aaa111"]

    async def test_extracts_multiple_works_without_crossing_links(self) -> None:
        outcome = await RuleExtractor().extract(
            "名称：剧集甲\n"
            "夸克：https://pan.quark.cn/s/a1\n"
            "阿里：https://www.alipan.com/s/a2\n"
            "名称：剧集乙\n"
            "夸克：https://pan.quark.cn/s/b1\n"
        )

        assert [i.title for i in outcome.items] == ["剧集甲", "剧集乙"]
        assert [[link.share_id for link in i.links] for i in outcome.items] == [
            ["a1", "a2"],
            ["b1"],
        ]

    async def test_detects_catalog_post_by_title(self) -> None:
        outcome = await RuleExtractor().extract(
            "名称：2026年8月25日 短剧更新目录\n夸克：https://pan.quark.cn/s/aaa111"
        )

        assert outcome.is_catalog is True
        assert outcome.items == []
        # 目录帖的链接不丢，进未归属队列
        assert len(outcome.unattributed_links) == 1

    async def test_normal_post_is_not_flagged_as_catalog(self) -> None:
        outcome = await RuleExtractor().extract(
            "名称：测试剧集\n夸克：https://pan.quark.cn/s/aaa111"
        )
        assert outcome.is_catalog is False

    async def test_is_deterministic(self) -> None:
        """同一输入两次产出必须完全一致，否则缓存与对照基准都失去意义。"""
        text = "名称：剧集甲\n夸克：https://pan.quark.cn/s/a1\n名称：剧集乙\n阿里：https://www.alipan.com/s/b1"
        first = await RuleExtractor().extract(text)
        second = await RuleExtractor().extract(text)

        assert [i.norm_key for i in first.items] == [i.norm_key for i in second.items]
        assert first.stats["links_attributed"] == second.stats["links_attributed"]

    async def test_reports_identity_for_cache_key(self) -> None:
        outcome = await RuleExtractor().extract("名称：剧集甲")
        assert (outcome.extractor_name, outcome.extractor_version) == ("rule", "v2")

    async def test_rehydrate_matches_fresh_extraction(self) -> None:
        text = "名称：剧集甲\n夸克：https://pan.quark.cn/s/a1"
        extractor = RuleExtractor()
        fresh = await extractor.extract(text)
        restored = extractor.rehydrate(fresh.raw_payload, text)

        assert [i.norm_key for i in restored.items] == [i.norm_key for i in fresh.items]
