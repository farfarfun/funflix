"""三个抽取器之间的行为一致性。

抽取器是可替换的实现（`extract/registry.py` 按来源类型分发，也能用
`--extractor` 强制指定）。可替换意味着**换一个抽取器不该改变同一条文本的
判定结果**，否则同样的输入会因为走了哪条路径而落出不同的数据，
而且没有任何东西会报错。

这里盯两件历史上真出过问题的事：
1. 目录帖判定曾经有两份各自演化的正则，同一个标题在 rule 与 sheet 下结论相反；
2. 标签抽取曾经只有 rule 做，换成 LLM 抽取器就悄悄丢光。
"""

from __future__ import annotations

import pytest

from funflix.services.extract.rule import RuleExtractor
from funflix.services.text.normalize import extract_tags, looks_like_catalog


class TestCatalogDetectionIsShared:
    """目录帖判定必须只有一份实现。

    判错的代价是脏数据：把「8月15日更新」当成剧名建进 media 表，
    之后每天都会多一部叫不同日期的"作品"，而且不会有任何报错。
    """

    @pytest.mark.parametrize(
        "title",
        [
            "更新30部",  # rule 那份漏了「更新N部」
            "8/15日更新",  # rule 那份的分隔符只认「月」，不认斜杠
            "8-15日更新",  # 也不认连字符
            "8月15号资源",  # 结尾也只认「日」，不认「号」
        ],
    )
    def test_rule_agrees_with_shared_detector(self, title: str) -> None:
        from funflix.services.extract.rule import _looks_like_catalog

        assert looks_like_catalog(title), f"共享判定应当认为 {title!r} 是目录帖"
        assert _looks_like_catalog(title, 1, 1), f"rule 抽取器漏判了 {title!r}"

    @pytest.mark.parametrize("title", ["目录", "合集", "打包", "片单", "资源包"])
    def test_common_markers_agree(self, title: str) -> None:
        from funflix.services.extract.rule import _looks_like_catalog

        assert looks_like_catalog(title)
        assert _looks_like_catalog(title, 1, 1)

    def test_normal_title_is_not_catalog(self) -> None:
        from funflix.services.extract.rule import _looks_like_catalog

        assert not looks_like_catalog("误杀2")
        assert not _looks_like_catalog("误杀2", 1, 1)

    def test_many_titles_one_link_still_counts_as_catalog(self) -> None:
        """rule 独有的启发式：标题远多于链接，是「罗列一堆片名给一个总链接」。"""
        from funflix.services.extract.rule import _looks_like_catalog

        assert _looks_like_catalog("普通剧名", 8, 1)


#: `extract_tags` 只认井号标签 —— 分享文案里的 `#悬疑` 是作者的明确标注，
#: 从简介里猜题材会大量误标。所以夹具得用真实文案里那种井号写法。
TAGGED_TEXT = """名称：误杀2
#悬疑 #犯罪 #中国大陆
链接：https://pan.quark.cn/s/abcd1234
"""


class TestTagsSurviveExtractorChoice:
    """标签不该因为换了抽取器就消失。

    `extract_tags` 是纯函数，任何抽取器拿到原文都能调用它。
    只有 rule 调用的话，换成「更好」的 LLM 抽取器反而会丢数据 ——
    `extract/runner.py` 会照常跑 `_upsert_tags`，只是每次都链接 0 个标签。
    """

    def test_the_shared_helper_finds_tags(self) -> None:
        """先确认原文里确实有标签可抽，否则下面的断言没有意义。"""
        assert extract_tags(TAGGED_TEXT), "夹具文本里应当能抽出标签"

    @pytest.mark.asyncio
    async def test_rule_extractor_produces_tags(self) -> None:
        outcome = await RuleExtractor().extract(TAGGED_TEXT)
        assert outcome.items
        assert outcome.items[0].tags, "rule 抽取器应当产出标签"

    @pytest.mark.asyncio
    async def test_sheet_extractor_produces_tags(self) -> None:
        from funflix.services.extract.sheet import SheetExtractor

        outcome = await SheetExtractor().extract(TAGGED_TEXT)
        if not outcome.items:
            pytest.skip("表格抽取器对这条自由文本没有产出，标签断言无从谈起")
        assert outcome.items[0].tags, "sheet 抽取器丢掉了标签"

    def test_llm_extractor_produces_tags(self) -> None:
        """标签用确定性正则从原文抽，不花 token 问模型。"""
        from funflix.services.extract.llm.extractor import parse_payload
        from funflix.services.text.linkscan import scan_links

        links = scan_links(TAGGED_TEXT)
        payload = {
            "is_catalog": False,
            "items": [{"title": "误杀2", "link_indexes": [0]}],
        }
        outcome = parse_payload(payload, links, TAGGED_TEXT)
        assert outcome.items[0].tags, "LLM 抽取器丢掉了标签"

    def test_llm_extractor_skips_tags_when_ambiguous(self) -> None:
        """一条文本含多部作品时不挂标签。

        无从知道 `#悬疑` 属于哪一部，全挂上等于把恐怖片的标签安到喜剧头上 ——
        污染的是筛选导航，宁可少标也不错标。
        """
        from funflix.services.extract.llm.extractor import parse_payload
        from funflix.services.text.linkscan import scan_links

        text = TAGGED_TEXT + "\n名称：另一部\n链接：https://pan.quark.cn/s/efgh5678\n"
        links = scan_links(text)
        payload = {
            "is_catalog": False,
            "items": [
                {"title": "误杀2", "link_indexes": [0]},
                {"title": "另一部", "link_indexes": [1]},
            ],
        }
        outcome = parse_payload(payload, links, text)
        assert all(not i.tags for i in outcome.items)
        assert outcome.stats.get("tags_skipped_multi_item") is True
