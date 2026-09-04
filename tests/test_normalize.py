from __future__ import annotations

import pytest

from funflix.base.enums import MediaType, Quality
from funflix.services.text.normalize import (
    clean_title,
    extract_episode_info,
    extract_quality,
    extract_size_bytes,
    extract_year,
    guess_media_type,
    norm_key,
    strip_title_marker,
)


class TestStripTitleMarker:
    @pytest.mark.parametrize(
        "line",
        [
            "名称：测试剧集",
            "片名: 测试剧集",
            "剧名：测试剧集",
            "资源名称：测试剧集",
            "1. 名称：测试剧集",
        ],
    )
    def test_removes_marker(self, line: str) -> None:
        assert strip_title_marker(line) == "测试剧集"

    def test_leaves_unmarked_line_untouched(self) -> None:
        assert strip_title_marker("测试剧集") == "测试剧集"


class TestCleanTitle:
    def test_strips_quality_and_subtitle_noise(self) -> None:
        assert clean_title("测试剧集 1080p 中字 WEB-DL") == "测试剧集"

    def test_strips_bracket_annotations(self) -> None:
        assert clean_title("【4K高码率】测试剧集（2024）") == "测试剧集"

    def test_strips_episode_counts(self) -> None:
        assert clean_title("测试剧集 全40集") == "测试剧集"
        assert clean_title("测试剧集 更新至20集") == "测试剧集"

    def test_handles_dotted_release_name(self) -> None:
        assert clean_title("Some.Title.2024.1080p.WEB-DL.H265") == "Some Title"

    def test_normalizes_fullwidth_to_halfwidth(self) -> None:
        assert clean_title("测试剧集　１０８０Ｐ") == "测试剧集"

    def test_strips_emoji(self) -> None:
        assert clean_title("📁 测试剧集 🏷") == "测试剧集"

    def test_keeps_season_which_identifies_the_work(self) -> None:
        """`第二季` 是作品身份的一部分，剥掉会把不同季错并成一部。"""
        assert "第二季" in clean_title("测试剧集第二季 1080p 中字")

    def test_keeps_numeric_sequel_marker(self) -> None:
        assert clean_title("测试剧集2 4K") == "测试剧集2"


class TestRealCorpusRegressions:
    """以下每条都对应一个在真实语料上暴露、而合成用例全绿时未能发现的缺陷。"""

    def test_type_prefix_is_stripped(self) -> None:
        """`电视剧：某剧` 与 `某剧` 必须归一到同一个键，否则会拆成两部作品。"""
        assert clean_title("电视剧：师兄太稳健 (2026)") == "师兄太稳健"
        assert norm_key("电视剧：师兄太稳健") == norm_key("师兄太稳健")

    def test_release_group_suffix_is_stripped(self) -> None:
        assert clean_title("寒衣入心（2026）4K S01E01 - E20 HiveWeb") == "寒衣入心"

    def test_standalone_category_tag_is_stripped(self) -> None:
        assert clean_title("一斩苍穹 (2026) 4K 更新至6集/国漫") == "一斩苍穹"

    def test_category_word_inside_a_title_is_kept(self) -> None:
        """只剥独立 token —— 否则《动画人生》会被洗成《人生》。"""
        assert clean_title("动画人生 1080p") == "动画人生"

    def test_full_date_is_removed_wholesale(self) -> None:
        """只抠年份会留下 `年8月25日` 这种残体。"""
        cleaned = clean_title("2026年8月25日 短剧更新目录")
        assert "年" not in cleaned and "8月" not in cleaned

    def test_season_marker_alone_implies_series(self) -> None:
        """判定前会先转小写，正则若只写大写 S 就永远匹配不上。"""
        assert guess_media_type("狂徒（2026）4K S01") == MediaType.TV
        assert guess_media_type("寒衣入心 S01E01 - E20") == MediaType.TV

    def test_title_signal_beats_body_signal(self) -> None:
        """正文简介常顺带提到"动画"，只看正文会把剧误判成动漫。"""
        body = "名称：某剧\n描述：讲述一位动画师的故事\n全40集"
        assert guess_media_type(body, title="某剧 全40集") == MediaType.TV


class TestNormKey:
    def test_collapses_spacing_and_case(self) -> None:
        assert norm_key("Some Title") == norm_key("some.title") == "sometitle"

    def test_same_work_with_different_noise_maps_to_same_key(self) -> None:
        assert norm_key("【4K】测试剧集 全40集 中字") == norm_key("测试剧集 1080p WEB-DL")

    def test_different_seasons_map_to_different_keys(self) -> None:
        """不同季必须区分开，否则资源会被错并。"""
        assert norm_key("测试剧集第一季") != norm_key("测试剧集第二季")

    def test_different_works_map_to_different_keys(self) -> None:
        assert norm_key("测试剧集") != norm_key("另一部剧")

    def test_drops_punctuation(self) -> None:
        assert norm_key("测试·剧集！") == norm_key("测试剧集")


class TestExtractYear:
    @pytest.mark.parametrize(
        ("text", "year"),
        [
            ("测试剧集 (2024)", 2024),
            ("测试剧集（1998）", 1998),
            ("Some.Title.2016.1080p", 2016),
            ("测试剧集 2024年", 2024),
        ],
    )
    def test_extracts_year(self, text: str, year: int) -> None:
        assert extract_year(text) == year

    def test_ignores_resolution_that_looks_like_a_year(self) -> None:
        """1920x1080 里的 1920 落在年份区间内，必须先剥分辨率再取年份。"""
        assert extract_year("测试剧集 1920x1080") is None

    def test_returns_none_without_year(self) -> None:
        assert extract_year("测试剧集 1080p") is None


class TestExtractQuality:
    @pytest.mark.parametrize(
        ("text", "quality"),
        [
            ("测试剧集 4K HDR", Quality.UHD_4K),
            ("测试剧集 2160p", Quality.UHD_4K),
            ("测试剧集 1080p", Quality.FHD_1080P),
            ("测试剧集 720p", Quality.HD_720P),
            ("测试剧集 480p", Quality.SD),
            ("测试剧集", Quality.UNKNOWN),
        ],
    )
    def test_detects_quality(self, text: str, quality: Quality) -> None:
        assert extract_quality(text) == quality


class TestExtractEpisodeInfo:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("测试剧集 全40集", "全40集"),
            ("测试剧集 更新至20集", "更新至20集"),
            ("Title S01E01-E12", "S01E01-E12"),
            ("Title EP01-EP12", "EP01-EP12"),
            ("季集：第2季 第3集", "第2季 第3集"),
        ],
    )
    def test_extracts_episode_info(self, text: str, expected: str) -> None:
        assert extract_episode_info(text) == expected

    def test_returns_none_for_movie(self) -> None:
        assert extract_episode_info("测试电影 1080p") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("大小：440.41MB", 461_803_356), ("7.49G", 8_042_326_261), ("未知", None)],
)
def test_extracts_size_bytes(text: str, expected: int | None) -> None:
    assert extract_size_bytes(text) == expected


class TestGuessMediaType:
    @pytest.mark.parametrize(
        ("text", "media_type"),
        [
            ("这是一部电影", MediaType.MOVIE),
            ("热播电视剧", MediaType.TV),
            ("经典动漫", MediaType.ANIME),
            ("综艺节目", MediaType.VARIETY),
            ("自然纪录片", MediaType.DOCUMENTARY),
        ],
    )
    def test_detects_by_keyword(self, text: str, media_type: MediaType) -> None:
        assert guess_media_type(text) == media_type

    def test_episode_count_implies_series(self) -> None:
        assert guess_media_type("测试剧集 全40集") == MediaType.TV

    def test_unknown_when_no_signal(self) -> None:
        """猜不出就是猜不出，不臆断为电影。"""
        assert guess_media_type("测试资源 1080p") == MediaType.UNKNOWN
