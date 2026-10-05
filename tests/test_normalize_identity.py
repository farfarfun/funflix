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
    block_key,
    clean_title,
    extract_episode_info,
    extract_season,
    extract_year,
    looks_like_junk_title,
    norm_key,
    series_norm_key,
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


class TestRealWorldSharingNoise:
    """用生产库里真实的脏标题兜一遍。

    这些样例全部来自「搜大主宰返回 448 条 media」那次排查 —— 同一部动漫
    被分享文案的集数、画质、更新状态、URL 碎片拆成了几百行。
    每多留一个字在归一键里，就多裂出一个假作品。
    """

    #: 左边是生产库里的原始 `media.title`，右边是期望的系列归一键。
    REAL_TITLES = [
        "国漫《大主宰》更至03",
        "大主宰年番2 4K高码 更新85集",
        "大主宰3D动画 第2季 国语 更至36 88集 含 版本 附第1季",
        "已更新 大主宰 第37集",
        "大主宰 更新至S01E93 杜比音效",
        "大主宰 60帧 中码率+4K 高码率",
        "大主宰 免费",
        "大主宰 S02 1080P",
        "D 大主宰 动漫版",
        "大主宰 每周自动更新",
        "大主宰 帧绮映画 杜比视界",
        "大主宰 87集",
        "大主宰 全104集",
    ]

    @pytest.mark.parametrize("title", REAL_TITLES)
    def test_all_collapse_to_one_series(self, title: str) -> None:
        assert series_norm_key(title) == "大主宰", f"{title!r} 没能并进《大主宰》"

    def test_they_really_are_one_group(self) -> None:
        """整组只能产出一个键 —— 这是「一部剧一条」的直接体现。"""
        assert len({series_norm_key(t) for t in self.REAL_TITLES}) == 1


class TestDifferentWorksMustStayApart:
    """误并比漏并严重得多。

    漏并还能再跑一轮归一救回来；误并之后两部作品的资源已经混在一行里，
    没有任何信息能把它们再分开。所以这些用例优先级高于上面的合并用例。
    """

    @pytest.mark.parametrize(
        "title",
        [
            "天命大主宰",
            "诛天大主宰",
            "北灵少年志之大主宰",
            "深空彼岸大主宰4",
            "从大主宰开始打卡",
        ],
    )
    def test_not_absorbed_into_dazhuzai(self, title: str) -> None:
        assert series_norm_key(title) != "大主宰", f"{title!r} 被错并进了《大主宰》"

    def test_sequels_are_distinct_works(self) -> None:
        """《误杀》和《误杀2》是两部电影，不是两季。"""
        assert series_norm_key("误杀") != series_norm_key("误杀2")

    def test_but_they_land_in_the_same_llm_block(self) -> None:
        """分块键要宽：续集同不同作品由 LLM 裁决，前提是它们出现在同一个候选组里。"""
        assert block_key("误杀") == block_key("误杀2")


class TestSeasonExtraction:
    """季号只在高置信写法上认，拿不准必须返回 None 交给 LLM。

    激进的抽季正则在真实数据上误报率不可接受：同一组标题里抽出过
    season=2/4/6/8/10，其中 4 和 10 是 `S01E04`、`年番 10` 这类集数误命中。
    抽错季号会把第 10 季凭空造出来，还会把真正的那一季的资源分走。
    """

    @pytest.mark.parametrize(
        ("title", "season"),
        [
            ("大主宰 第2季", 2),
            ("大主宰 第二季", 2),
            ("大主宰年番2", 2),
            ("大主宰 S02", 2),
            ("大主宰II", 2),
            ("Stranger Things S03", 3),
        ],
    )
    def test_high_confidence_seasons(self, title: str, season: int) -> None:
        assert extract_season(title) == season

    @pytest.mark.parametrize(
        "title",
        [
            # 集数，不是季号
            "大主宰年番 10",
            "大主宰 更新至S01E93",
            "大主宰 87集",
            # 片名自带的数字/罗马数字，不是季号
            "误杀2",
            "第八号当铺",
        ],
    )
    def test_ambiguous_seasons_are_left_to_the_llm(self, title: str) -> None:
        assert extract_season(title) is None, f"{title!r} 抽出了一个不该有的季号"


class TestJunkTitles:
    """抓成标题的页面文案不是作品。

    生产库里 `夸克` 45888 行、`链接` 10771 行、`查看资源` 6177 行、
    `磁力下载` 5636 行 —— 都是网盘列名和按钮文案被 `find_title` 的回退
    当成了剧名。它们必须在建 `ExtractedItem` 之前就被拦掉。
    """

    @pytest.mark.parametrize(
        "title",
        [
            "夸克",
            "夸克网盘",
            "百度",
            "光鸭云盘",
            "链接",
            "社区链接",
            "查看资源",
            "磁力下载",
            "浏览",
            "大小",
            "提取码",
            "失效补档",
            "资源",
            "标题",
            "84",
            "007803",
            "adg4qmqwc8j",
            "",
        ],
    )
    def test_detected_as_junk(self, title: str) -> None:
        assert looks_like_junk_title(clean_title(title)) is True

    @pytest.mark.parametrize(
        "title",
        [
            "大主宰",
            "误杀2",
            "更上一层楼",
            "变更",
            "动画人生",
            "DIY天堂",
            "第八号当铺",
            "Oppenheimer",
            "leoziyuan 的日常",
            "三体",
        ],
    )
    def test_real_titles_survive(self, title: str) -> None:
        assert looks_like_junk_title(clean_title(title)) is False, f"{title!r} 被误判成垃圾"


class TestScrapedPageDumpCollapses:
    """整个豆瓣页面被采成标题的那一类行。

    生产库里这种行清洗完还剩五百多字，每一条都算出独一无二的键 ——
    长尾单行组里占比最大的一类就是它。见 `_SCRAPE_CUT_RE`。
    """

    DOUBAN_DUMP = (
        "描述:少年江湖 少年江湖导演: 麦咏麟编剧: 左曌阳主演: 敖瑞鹏 邓超元 宗元圆 "
        "四正 蒲雨童 吴一逊类型: 剧情 爱情 古装制片国家 地区: 中国大陆语言: "
        "汉语普通话首播: 集数: 24单集片长: 45分钟又名: 我才不要当盟主少年江湖的"
        "剧情简介 · · · · · · 女主小如是只会点鸡毛蒜皮武艺的江湖小白。提取码: AR9R"
    )

    def test_dump_collapses_to_the_bare_title(self) -> None:
        assert series_norm_key(self.DOUBAN_DUMP) == "少年江湖"

    def test_dump_merges_with_the_clean_title(self) -> None:
        assert series_norm_key(self.DOUBAN_DUMP) == series_norm_key("少年江湖 24集")

    def test_repeated_title_is_collapsed(self) -> None:
        assert clean_title("少年江湖 少年江湖") == "少年江湖"

    @pytest.mark.parametrize(
        "title",
        [
            # 短字段名必须带冒号才算字段名，否则会把片名正文啃掉。
            # 这两条都是生产库里的真实标题。
            "导演万岁",
            "什么叫我都元婴期了还要哄着大小姐",
        ],
    )
    def test_field_names_inside_a_real_title_are_kept(self, title: str) -> None:
        assert clean_title(title) == title

    def test_degenerate_repetition_is_not_collapsed(self) -> None:
        """`aaaa|aaaa` 不是采集重复，折叠它会把长标题啃成十来个字符。"""
        assert clean_title("a" * 600) == "a" * 500


class TestForeignAliasDoesNotSplitTheWork:
    """外文原名在不在、写不写全，不该让同一部片裂成好几行。"""

    @pytest.mark.parametrize(
        "title",
        [
            "美国狙击手",
            "《美国狙击手》4K原盘REMUX",
            "美国狙击手 4K原盘REMUX 国英双音 字幕",
            ": 美国狙击手 American Sniper 年传记片",
            "美国狙击手 American Sniper",
        ],
    )
    def test_all_collapse_to_the_chinese_title(self, title: str) -> None:
        assert series_norm_key(title) == "美国狙击手"

    def test_japanese_original_title_is_dropped(self) -> None:
        assert series_norm_key("狼的孩子雨和雪 おおかみこどもの雨と雪") == "狼的孩子雨和雪"

    @pytest.mark.parametrize(
        "title,expected",
        [
            # 片名本体就是拉丁字符时不能剥 —— 剥完只剩空串，宁可不并。
            ("Dopesick", "dopesick"),
            ("Oppenheimer", "oppenheimer"),
            # 片名里的单个拉丁字母是片名的一部分，不是外文原名。
            ("K歌情人", "k歌情人"),
            ("X战警", "x战警"),
            ("Z风暴", "z风暴"),
            ("3D肉蒲团", "3d肉蒲团"),
            ("4K先生", "4k先生"),
        ],
    )
    def test_latin_in_the_title_itself_is_kept(self, title: str, expected: str) -> None:
        assert series_norm_key(title) == expected

    def test_norm_key_keeps_the_alias(self) -> None:
        """外文原名要留给 `Work.original_title` / `aliases`，所以只有
        `series_norm_key` 摘它，`norm_key` 不动 —— 后者还绑着 `uq_media_identity`。
        """
        assert "americansniper" in norm_key("美国狙击手 American Sniper")


class TestSheetColumnArtifacts:
    """某个表格源把列名、行号、演员列全采进了标题。见 `_SHEET_MARKER_RE`。"""

    @pytest.mark.parametrize(
        "title,expected",
        [
            (
                "#000000 28 text 什么叫我都元婴期了还要哄着大小姐",
                "什么叫我都元婴期了还要哄着大小姐",
            ),
            ("text 25 重获新生:母亲的逆袭 鞠瑾&罗大雪", "重获新生母亲的逆袭"),
            ("侯门老祖宗重整家族荣耀 张萓紘&朱诗妤 text", "侯门老祖宗重整家族荣耀"),
            ("80 text #000000 软糯参孙全家宠", "软糯参孙全家宠"),
        ],
    )
    def test_artifacts_are_stripped(self, title: str, expected: str) -> None:
        assert series_norm_key(title) == expected

    def test_row_number_strip_requires_a_sheet_marker(self) -> None:
        """没有表格标记时，独立数字是季号而不是行号，绝不能剥。"""
        assert series_norm_key("大主宰 2") == "大主宰2"
        assert series_norm_key("误杀 2") == "误杀2"


class TestRegionAndGenreCompounds:
    """来源站点的分类列会跟题材词连写，`古装大陆片` 整体是一个 token。"""

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("资源的名称:古装大陆片 春家小姐是讼师", "春家小姐是讼师"),
            ("#上海女子图鉴 #女性成长 #国产剧 #职场", "上海女子图鉴女性成长职场"),
            # 来源模板是「片名 + 外文名 + 年份 + 题材片」，而年份字段经常是空的，
            # 于是留下一个光杆「年」粘在题材词上。
            (": 旋风九日 年传记片", "旋风九日"),
            (": 御赐小仵作 年剧情片", "御赐小仵作"),
        ],
    )
    def test_compounds_are_stripped(self, title: str, expected: str) -> None:
        assert series_norm_key(title) == expected


class TestProviderShareIdIsJunk:
    """URL 被拆碎之后只剩「网盘名 + 一串 hash」。见 `_PROVIDER_SHARE_ID_RE`。"""

    @pytest.mark.parametrize(
        "title",
        [
            "夸克 :https: pan quark cn s 00ab4389973f",
            "查看资源 351f1a2ad335",
            # 网盘客户端的安装包，不是影视作品
            "夸克 6 5 2 338 清爽版 apk",
        ],
    )
    def test_detected_as_junk(self, title: str) -> None:
        assert looks_like_junk_title(clean_title(title)) is True


class TestDecorativeSeparatorsAndRepeats:
    """分享者自造的分隔符与重复串接。"""

    def test_han_vertical_bar_is_a_separator(self) -> None:
        """`丨` 是汉字（U+4E28），不是标点也不是空白 —— 不显式列出就切不开，
        后面的 token 级噪声剔除也就永远碰不到被它粘住的画质词。
        """
        assert series_norm_key("大主宰丨丨1080p丨") == "大主宰"

    def test_doubled_three_character_title_collapses(self) -> None:
        assert series_norm_key("大主宰大主宰") == "大主宰"

    @pytest.mark.parametrize(
        "title",
        ["高高兴兴", "马马虎虎", "妈妈", "看了看", "好好好", "步步惊心", "日日是好日"],
    )
    def test_normal_reduplication_is_kept(self, title: str) -> None:
        """汉语叠词是正常构词，折叠它就把片名洗烂了。"""
        assert clean_title(title) == title

    #: 生产库里真实出现 17 行，站点把片名渲染了三遍、其中一遍带续作号。
    REPEATED_SCRAPE = "大主宰 第二季 大主宰2 大主宰 年番2 更新EP71"

    def test_repeated_title_with_sequel_number_collapses(self) -> None:
        """重复采集的 `大主宰2` 里那个光杆 `2` 要摘掉。

        不摘的话键算成 `大主宰2大主宰`，这 17 行凭空变成一个独立 Work。
        """
        assert series_norm_key(self.REPEATED_SCRAPE) == "大主宰"

    def test_repeated_title_still_yields_its_season(self) -> None:
        """并进《大主宰》之后季号不能丢 —— 它是第 2 季，不是无季。"""
        assert extract_season(self.REPEATED_SCRAPE) == 2

    @pytest.mark.parametrize(
        "title",
        ["速度与激情 速度与激情9", "误杀 误杀2", "大主宰 大主宰2"],
    )
    def test_sequel_number_survives_without_a_repeat(self, title: str) -> None:
        """片名只出现一次时，末尾数字是续作号，摘掉就是不可逆的误并。

        合集文案「A A9」里 A9 是独立一部。只有片名**本身**重复出现
        （≥2 次）才算重复采集的指纹，见 `_collapse_repeated_tokens`。
        """
        base = series_norm_key(title.split()[0])
        assert series_norm_key(title) != base


class TestBitrateStringsStripWhole:
    """码率串必须整条剥干净，不能留下孤零零的「率」。"""

    @pytest.mark.parametrize(
        "title",
        [
            "大主宰年番 4K高码率版",
            "大主宰年番 4K高码率",
            "大主宰 4KHDR高码",
            "中码率+4K 大主宰",
            "大主宰 低码版",
        ],
    )
    def test_bitrate_variants_all_collapse(self, title: str) -> None:
        """`4k高码` / `高码率` / `高码` 当年是三个并列字面量，而正则的选择分支
        是「出现得早的赢」不是「最长的赢」：`4K高码率版` 在位置 0 就被
        `4k高码` 吃掉，剩下 `率版` —— 生产库里 29 条资源就这么挂到了一个
        叫《大主宰 率版》的 Work 上。
        """
        assert series_norm_key(title) == "大主宰"

    def test_bitrate_pattern_does_not_eat_real_titles(self) -> None:
        """`版` 只跟着码率串一起剥，光杆的不碰 —— 不然《盗版时代》就没了。"""
        assert series_norm_key("盗版时代") == "盗版时代"


class TestCastColumnIsNotPartOfTheTitle:
    """短剧源把演员列串在片名后面，用 `&` 连接。见 `_CAST_AMP_RE`。"""

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("诱引 刘擎&白妍", "诱引"),
            ("我凭本事单身 宋伊人&邓超元", "我凭本事单身"),
            ("陆机长航线前方有心动预警 李佳琛 &鲁紫萱", "陆机长航线前方有心动预警"),
            ("少年江湖 敖瑞鹏&邓超元 国产剧", "少年江湖"),
            ("侯门老祖宗重整家族荣耀 张萓紘&朱诗妤 text", "侯门老祖宗重整家族荣耀"),
            # 中间的 `&` 连的是两个别名，只有结尾那一串才是演员
            ("在万米高空说爱你&爱在万米高空 时童&谢卓", "在万米高空说爱你&爱在万米高空"),
        ],
    )
    def test_cast_is_stripped(self, title: str, expected: str) -> None:
        assert clean_title(title) == expected

    @pytest.mark.parametrize("title", ["泰坦尼克号&阿凡达", "超人&蝙蝠侠", "银魂番剧&漫画"])
    def test_does_not_bite_into_the_title(self, title: str) -> None:
        """没有 `(?:^|\\s)` 锚的话，`泰坦尼克号&阿凡达` 会被从中间咬住
        `坦尼克号&阿凡达`，剥完只剩一个「泰」。整条被吃光时退回原串。
        """
        assert clean_title(title) == title
