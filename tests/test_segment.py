from __future__ import annotations

from funflix.base.enums import Provider
from funflix.services.text.segment import segment_text


class TestSingleWork:
    def test_one_title_one_link(self) -> None:
        result = segment_text("名称：测试剧集\n夸克：https://pan.quark.cn/s/aaa111")

        assert len(result.segments) == 1
        assert result.segments[0].title_raw == "测试剧集"
        assert [link.share_id for link in result.segments[0].links] == ["aaa111"]
        assert result.unattributed_links == []

    def test_one_title_many_links(self) -> None:
        """同一部作品分享到多个网盘 —— 三个链接都归属到这一部。"""
        result = segment_text(
            "名称：测试剧集\n"
            "夸克：https://pan.quark.cn/s/aaa111\n"
            "阿里：https://www.alipan.com/s/bbb222\n"
            "百度：https://pan.baidu.com/s/1ccc333\n"
        )

        assert len(result.segments) == 1
        links = result.segments[0].links
        assert [link.provider for link in links] == [
            Provider.QUARK,
            Provider.ALIPAN,
            Provider.BAIDU,
        ]


class TestMultipleWorks:
    def test_two_titles_each_with_own_link(self) -> None:
        result = segment_text(
            "名称：剧集甲\n"
            "夸克：https://pan.quark.cn/s/aaa111\n"
            "名称：剧集乙\n"
            "夸克：https://pan.quark.cn/s/bbb222\n"
        )

        assert [s.title_raw for s in result.segments] == ["剧集甲", "剧集乙"]
        assert [s.links[0].share_id for s in result.segments] == ["aaa111", "bbb222"]

    def test_multiple_works_each_with_multiple_links(self) -> None:
        """多作品 × 多链接：归属不能串台。"""
        result = segment_text(
            "名称：剧集甲\n"
            "夸克：https://pan.quark.cn/s/a1\n"
            "阿里：https://www.alipan.com/s/a2\n"
            "名称：剧集乙\n"
            "夸克：https://pan.quark.cn/s/b1\n"
            "阿里：https://www.alipan.com/s/b2\n"
            "名称：剧集丙\n"
            "夸克：https://pan.quark.cn/s/c1\n"
        )

        assert len(result.segments) == 3
        assert [[link.share_id for link in s.links] for s in result.segments] == [
            ["a1", "a2"],
            ["b1", "b2"],
            ["c1"],
        ]
        assert result.attributed_count == len(result.all_links) == 5

    def test_every_link_is_attributed_or_reported(self) -> None:
        """不变式：扫到的链接要么归属到某段，要么进 unattributed，绝不静默丢失。"""
        text = "https://pan.quark.cn/s/orphan\n名称：剧集甲\n夸克：https://pan.quark.cn/s/aaa111\n"
        result = segment_text(text)

        assert result.attributed_count + len(result.unattributed_links) == len(result.all_links)
        assert [link.share_id for link in result.unattributed_links] == ["orphan"]

    def test_title_and_link_on_same_line(self) -> None:
        result = segment_text(
            "名称：剧集甲 https://pan.quark.cn/s/a1\n名称：剧集乙 https://pan.quark.cn/s/b1"
        )
        assert len(result.segments) == 2
        assert [s.links[0].share_id for s in result.segments] == ["a1", "b1"]


class TestFallbackStrategies:
    def test_bracket_headings_split_works(self) -> None:
        result = segment_text(
            "【剧集甲】\nhttps://pan.quark.cn/s/a1\n【剧集乙】\nhttps://pan.quark.cn/s/b1"
        )
        assert [s.title_raw for s in result.segments] == ["剧集甲", "剧集乙"]

    def test_numbered_list_splits_works(self) -> None:
        result = segment_text(
            "1. 剧集甲\nhttps://pan.quark.cn/s/a1\n2. 剧集乙\nhttps://pan.quark.cn/s/b1"
        )
        assert [s.title_raw for s in result.segments] == ["剧集甲", "剧集乙"]

    def test_blank_line_paragraphs_split_works(self) -> None:
        result = segment_text(
            "剧集甲\nhttps://pan.quark.cn/s/a1\n\n剧集乙\nhttps://pan.quark.cn/s/b1"
        )
        assert [s.title_raw for s in result.segments] == ["剧集甲", "剧集乙"]

    def test_text_without_any_marker_becomes_one_segment(self) -> None:
        result = segment_text("就一部剧\nhttps://pan.quark.cn/s/a1")
        assert len(result.segments) == 1
        assert result.segments[0].links[0].share_id == "a1"


class TestOverSplitGuards:
    def test_numbered_description_bullets_do_not_split(self) -> None:
        """真实语料里的坑：描述字段里的 `3.穿书` 长得像标题标记。

        强标记 `名称：` 存在时只按它切，编号行一律无视，
        否则链接会被归属到 "3.穿书" 这种简介片段上。
        """
        result = segment_text(
            "名称：测试剧集\n"
            "描述：\n"
            "1. 穿书\n"
            "2. 复仇\n"
            "3. 团宠\n"
            "夸克：https://pan.quark.cn/s/aaa111\n"
        )

        assert len(result.segments) == 1
        assert result.segments[0].title_raw == "测试剧集"
        assert result.segments[0].links[0].share_id == "aaa111"

    def test_numbered_bullets_without_strong_marker_still_guarded(self) -> None:
        """没有强标记时，编号行也要满足「至少两段各自带链接」才算作品边界。

        这里只有一个链接，说明编号是简介排版而非作品列表。
        """
        result = segment_text(
            "某剧简介\n1. 穿书\n2. 复仇\n3. 团宠\n链接：https://pan.quark.cn/s/aaa111\n"
        )

        assert len(result.segments) == 1
        assert result.segments[0].links[0].share_id == "aaa111"

    def test_paragraphs_without_links_do_not_force_split(self) -> None:
        result = segment_text(
            "频道公告\n欢迎关注\n\n名称：剧集甲\n夸克：https://pan.quark.cn/s/a1\n"
        )
        # 强标记存在，公告段落不会被当成作品
        assert [s.title_raw for s in result.segments] == ["剧集甲"]


class TestEdgeCases:
    def test_empty_text(self) -> None:
        result = segment_text("")
        assert result.all_links == []
        assert result.attributed_count == 0

    def test_text_with_no_links(self) -> None:
        result = segment_text("名称：测试剧集\n描述：暂无资源")
        assert len(result.segments) == 1
        assert result.segments[0].links == []
