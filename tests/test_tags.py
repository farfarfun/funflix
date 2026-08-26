from __future__ import annotations

import pytest

from funflix.services.text.normalize import classify_tag, extract_tags, tag_norm_key


class TestTagNormKey:
    def test_collapses_spacing_and_case(self) -> None:
        assert tag_norm_key("科 幻") == tag_norm_key("科幻") == "科幻"

    def test_drops_punctuation(self) -> None:
        assert tag_norm_key("悬疑！") == "悬疑"


class TestClassifyTag:
    @pytest.mark.parametrize("name", ["剧情", "悬疑", "科幻", "古装", "甜宠"])
    def test_whitelisted_words_are_genres(self, name: str) -> None:
        assert classify_tag(name) == "genre"

    @pytest.mark.parametrize("name", ["国产", "美国", "日本"])
    def test_region_words(self, name: str) -> None:
        assert classify_tag(name) == "region"

    @pytest.mark.parametrize("name", ["国语", "粤语"])
    def test_language_words(self, name: str) -> None:
        assert classify_tag(name) == "language"

    @pytest.mark.parametrize("name", ["2024", "1998", "90年代"])
    def test_year_words(self, name: str) -> None:
        assert classify_tag(name) == "year"

    @pytest.mark.parametrize("name", ["吞噬星空", "蝉", "无损音乐", "某个新词"])
    def test_unknown_words_go_to_other_not_genre(self, name: str) -> None:
        """真实语料里作者会把剧名也打成井号标签。

        当成题材会在筛选导航里堆出一堆只对应一部作品的假分类 ——
        题材导航被污染就没法用了，而 other 里的标签随时能再捞出来。
        """
        assert classify_tag(name) == "other"


class TestExtractTags:
    def test_extracts_hashtags_with_dimension(self) -> None:
        tags = extract_tags("名称：某剧\n标签：#剧情 #国产 #2024")
        assert tags == [("genre", "剧情"), ("region", "国产"), ("year", "2024")]

    def test_series_name_hashtag_is_not_a_genre(self) -> None:
        tags = extract_tags("#剧情 #吞噬星空")
        assert ("genre", "剧情") in tags
        assert ("other", "吞噬星空") in tags

    def test_deduplicates_repeated_hashtags(self) -> None:
        assert extract_tags("#科幻 开头 #科幻 结尾 #科幻") == [("genre", "科幻")]

    def test_hashtag_stops_at_whitespace(self) -> None:
        """井号标签不跨空格 —— `#科 幻` 是标签「科」加普通文字，不是「科幻」。

        跨空格合并只发生在入库时的 tag_norm_key 层面。
        """
        assert extract_tags("#科 幻") == [("other", "科")]

    def test_skips_channel_promo_stopwords(self) -> None:
        """频道自宣和操作提示不是作品分类。"""
        tags = extract_tags("#剧情 #转存 #夸克 #投稿")
        assert tags == [("genre", "剧情")]

    def test_text_without_hashtags_yields_nothing(self) -> None:
        assert extract_tags("名称：某剧\n链接：https://pan.quark.cn/s/a1") == []

    def test_does_not_guess_genre_from_prose(self) -> None:
        """只取作者的明确标注，不从简介里猜 —— 猜会大量误标。"""
        assert extract_tags("这是一部悬疑推理题材的剧集") == []
