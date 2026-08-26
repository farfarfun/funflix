"""剧名归一里决定「作品身份」的部分。

`_upsert_media` 按 `(norm_key, media_type, year)` 三元组认作品，所以
`clean_title` / `norm_key` / `extract_year` 的每一个取舍都直接决定了
两条分享是被合成一部、还是被拆成两部。两个方向都会出错：

- 剥得太狠 → 不同作品被错并（第一季和第二季变成同一部，链接混在一起）
- 留得太多 → 同一作品被拆开（同一部片按发帖日期裂成好几个 media）

这两类都不会报错，只会让库里的数据慢慢变得没法用。
"""

from __future__ import annotations

import pytest

from funflix.services.text.normalize import (
    clean_title,
    extract_episode_info,
    extract_year,
    norm_key,
)


def key(title: str) -> str:
    return norm_key(clean_title(title))


class TestSeasonIsPartOfIdentity:
    """季号是作品身份的一部分，中英文都得留住。

    `clean_title` 的文档说得很清楚：刻意保留 `第N季`，剥掉会把
    《某剧 第一季》和《某剧 第二季》错并成同一部。但 `S01` 这种写法
    曾经被当成集数噪声剥掉，于是英文剧名走的是被错并的那条路 ——
    同一个规则，中文对、英文错。
    """

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Stranger Things S01", "Stranger Things S02"),
            ("Stranger Things S1", "Stranger Things S2"),
            ("怪奇物语 第一季", "怪奇物语 第二季"),
        ],
    )
    def test_two_seasons_are_two_works(self, a: str, b: str) -> None:
        assert key(a) != key(b), f"{a!r} 与 {b!r} 归一到了同一个 key，会被合并成一部作品"

    def test_season_two_survives_cleaning(self) -> None:
        assert "S02" in clean_title("Stranger Things S02")

    def test_season_one_is_implicit(self) -> None:
        """第一季视为隐含默认：`某剧 S01` 与 `某剧` 是同一部。

        绝大多数剧只有一季，真实语料里 `某剧 S01E01-E20` 与 `某剧 全20集`
        指的是同一部；留着 S01 会把它们拆成两部。S02 起才真正区分身份。
        """
        assert key("Stranger Things S01") == key("Stranger Things")
        assert key("寒衣入心（2026）4K S01E01 - E20 HiveWeb") == key("寒衣入心")

    def test_episode_marker_is_still_noise(self) -> None:
        """`S01E05` 是「第几集」，不是作品身份，照旧要剥掉。"""
        assert key("Stranger Things S01E05") == key("Stranger Things S01")

    def test_season_two_episode_keeps_the_season(self) -> None:
        """`S02E05` 要洗成 S02 —— 剥光了就跟第一季混在一起了。"""
        assert key("Stranger Things S02E05") == key("Stranger Things S02")

    def test_season_is_still_reported_as_episode_info(self) -> None:
        """从标题里剥不剥，和能不能识别出来，是两件事。"""
        assert extract_episode_info("Stranger Things S02") is not None


class TestPostingDateIsNotTheReleaseYear:
    """帖子里的「更新日期」不是作品的上映年份。

    `clean_title` 会把整个日期剥掉，`extract_year` 却只剥分辨率 ——
    于是同一部片，8 月发的那条 year=2025、12 月发的那条 year=2024，
    三元组不同，裂成两个 media，链接各分一半。
    """

    @pytest.mark.parametrize(
        "title",
        [
            "复仇者联盟 2025年8月25日更新",
            "复仇者联盟 2024年12月31日更新",
            "复仇者联盟 2023/07/01 更新",
        ],
    )
    def test_update_date_is_not_taken_as_year(self, title: str) -> None:
        assert extract_year(title) is None, f"{title!r} 里的日期被当成了上映年份"

    def test_same_show_posted_on_different_days_stays_one_work(self) -> None:
        a = "复仇者联盟 2025年8月25日更新"
        b = "复仇者联盟 2024年12月31日更新"
        assert (key(a), extract_year(a)) == (key(b), extract_year(b))

    def test_real_release_year_still_recognised(self) -> None:
        """把日期剥掉不能连真年份一起剥掉。"""
        assert extract_year("复仇者联盟 (2012)") == 2012
        assert extract_year("流浪地球2 2023") == 2023

    def test_year_alongside_a_date(self) -> None:
        """既有上映年份又有更新日期时，要取上映年份。"""
        assert extract_year("复仇者联盟 (2012) 2025年8月25日更新") == 2012
