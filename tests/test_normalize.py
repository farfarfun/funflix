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
    looks_like_junk_title,
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

    def test_limits_title_to_database_column_length(self) -> None:
        assert clean_title("a" * 600) == "a" * 500


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

    def test_book_field_labels_are_cut(self) -> None:
        """小说分享把作者名整条粘进片名，生产库里有 4,399 行。"""
        assert clean_title("全民攻防:我有签到系统 作者:奏光 txt") == "全民攻防:我有签到系统"
        assert clean_title("某书名 译者:李四 出版社:人民文学") == "某书名"

    @pytest.mark.parametrize(
        "title",
        [
            "9夜王 导演版 蓝光原盘",  # 版本名
            "MobLand 黑帮领地 盖里奇 导演",  # 署名后置
            "PowerDirector 威力导演视频剪辑 v14 5 0",  # 软件名
            "▎今敏导演",
        ],
    )
    def test_field_label_without_colon_is_not_cut(self, title: str) -> None:
        """冒号是 `_SCRAPE_CUT_RE` 唯一的精度来源。

        生产库 926 行含「导演」的标题里有 670 行不带冒号，全都不该切 ——
        少了这道约束，《导演万岁》《威力导演》会被洗成空串。
        """
        assert "导演" in clean_title(title)

    def test_field_label_at_the_very_start_is_not_cut(self) -> None:
        """`:导演你有病` 是真片名。靠 `strip_scrape_labels()` 的 start < 2 闸挡住。"""
        assert clean_title(":导演你有病 導演你有病") == "导演你有病 導演你有病"

    def test_year_with_its_suffix_is_stripped_before_tokenizing(self) -> None:
        """`2001年剧情片` 整条剥掉，不能只摘走年份。

        `_YEAR_RE` 排在 token 级剔除**之后**，摘走 `2001` 就收工了，留下的
        `年剧情片` 再也没人看一眼 —— 而它其实就在 `_TOKEN_NOISE` 里，只是
        轮不到。生产库 83,820 行 work 里有 17,753 行（21%）顶着这个残渣，
        每个写法单独裂一行。见 `_YEAR_SUFFIX_RE`。
        """
        assert clean_title("某片 2001年剧情片") == "某片"
        assert clean_title("1998年香港电影 喜剧片 整容日记") == "整容日记"
        # 季号照旧保留 —— 它是作品身份的一部分
        assert clean_title("某剧 2024年 第2季") == "某剧 第2季"

    def test_bare_year_character_is_kept(self) -> None:
        """`_YEAR_SUFFIX_RE` 只认带四位年份的写法：光杆「年」是正常汉字。"""
        assert clean_title("年会不能停") == "年会不能停"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("真心半解 8 0分", "真心半解"),  # 小数点在前面已被换成空格
            ("莲花楼 豆瓣8 5分", "莲花楼"),
            ("某片 7.5分", "某片"),
        ],
    )
    def test_rating_is_stripped(self, raw: str, expected: str) -> None:
        """评分不是作品身份：同一部片评分变一下就多裂一行。生产库里 1,603 行。"""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize("title", ["分手大师", "某纪录片 120分钟", "第9分队", "三分之一"])
    def test_rating_rule_does_not_eat_other_uses_of_the_character(self, title: str) -> None:
        """「分」前面有数字还不够，后面跟着量词就不是评分。见 `_RATING_RE`。"""
        assert "分" in clean_title(title)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("刀锋战士13部全", "刀锋战士"),
            ("敢死队4部", "敢死队"),
            ("黑客帝国 4部全", "黑客帝国"),
            # 前导的 `共` 要一起吃掉，不然留个尾巴
            ("某系列 共5部", "某系列"),
            # 整条都是元信息的那 20 行，洗成空串交给 `looks_like_junk_title` 判删
            ("共16部 ,2部", ""),
        ],
    )
    def test_boxset_quantifier_is_stripped(self, raw: str, expected: str) -> None:
        """「打包了几部」不是作品身份 —— `刀锋战士13部全` 自己占了一个 work。"""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize("title", ["西游记 第3部", "第 3部 某片", "俱乐部风云", "第三部曲"])
    def test_boxset_rule_keeps_the_part_number(self, title: str) -> None:
        """`第N部` 必须留下：它跟 `第N季` 一样是作品身份的一部分。"""
        assert "部" in clean_title(title)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("◎译 名 曼哈顿 曼克顿", "曼哈顿 曼克顿"),
            # 整页字段：挑 `◎片 名` 的值，后面的元数据字段全扔掉
            ("◎片 名 大话西游之月光宝盒 ◎年 代 1995 ◎产 地 中国香港", "大话西游之月光宝盒"),
            # 片名字段不在第一个也要挑对（全角空格被 NFKC 换成普通空格）
            ("◎年　代 1995 ◎译　名 大话西游 ◎产　地 中国香港", "大话西游"),
            # 第一个 `◎` 前面已经有片名 —— 按截断处理
            ("大话西游 ◎年 代 1995 ◎产 地 中国香港", "大话西游"),
        ],
    )
    def test_dump_fields_keep_only_the_title_field(self, raw: str, expected: str) -> None:
        """`◎` 打头的等宽字段页不带冒号，`_SCRAPE_CUT_RE` 一条都切不到。

        生产库里 3,884 行 work 把整页字段串成一行当标题存着。见 `_cut_dump_fields`。
        """
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["误杀 简英", "某片 繁英", "某片 国日英多", "某片 简 幕", "某片 英特效"]
    )
    def test_language_combination_token_is_stripped(self, raw: str) -> None:
        """语言/字幕组合是**开放**的，穷举字面量永远补不齐。见 `_LANG_COMBO_RE`。"""
        assert clean_title(raw) in {"误杀", "某片"}

    @pytest.mark.parametrize("title", ["中国机长", "英雄本色", "简爱", "三国演义", "中国 蓝盔"])
    def test_language_rule_is_token_level_not_substring(self, title: str) -> None:
        """这些片名的头两个字正好落在那个字符集里，按子串剥会洗烂。"""
        assert clean_title(title) == title

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("No 027 唐伯虎点秋香", "唐伯虎点秋香"),
            ("No 1024 狂飙", "狂飙"),
            # 锚在 `^` 上，行中的 `No` 不动；`Nobody` 后面没有数字，对不上
            ("Room No 237", "Room No 237"),
            ("Nobody 的故事", "Nobody 的故事"),
        ],
    )
    def test_list_number_prefix_is_stripped(self, raw: str, expected: str) -> None:
        """清单序号 `No 027`，生产库里 266 行且全部在行首。"""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("金钱游戏 全", "金钱游戏"),  # 全集 / 全40集 被集数正则啃掉一半
            ("小心许愿 硬", "小心许愿"),  # 硬字幕
            ("老九门 版", "老九门"),  # 修复版 / 蓝光版
            ("疾速反击 补", "疾速反击"),  # 补档
            ("翘楚 完", "翘楚"),  # 完结
            ("种群 2G 集", "种群"),
            ("好奇号 杂志 年", "好奇号 杂志"),
            ("切尔诺贝利 DV&HDR 特效", "切尔诺贝利"),  # 硬字幕特效
        ],
    )
    def test_single_character_noise_residue_is_stripped(self, raw: str, expected: str) -> None:
        """复合噪声词被啃掉一半剩下的光杆单字，这八个字挂着 853 行 work。"""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "title",
        ["完美世界", "盗版时代", "集结号", "超能陆战队", "声之形", "全职高手", "补习班"],
    )
    def test_single_character_rule_is_token_level_not_substring(self, title: str) -> None:
        """上一条只在那个字**独立成 token** 时生效，否则这些片名全得洗烂。"""
        assert clean_title(title) == title

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("雷霆扫毒 11 3GB", "雷霆扫毒"),  # 小数点在前面已被换成空格
            ("实测给了我2TB", "实测给了我"),
            ("某片 2GB", "某片"),
        ],
    )
    def test_file_size_is_stripped(self, raw: str, expected: str) -> None:
        """体积不是作品身份。左边界不能用 `(?<!\\w)` —— `\\w` 在 Python 里含汉字。"""
        assert clean_title(raw) == expected

    def test_size_rule_does_not_eat_men_in_black(self) -> None:
        """单位刻意不收 `MiB` —— 整条正则带 `IGNORECASE`，收了 `3 MIB` 就被当体积。"""
        assert clean_title("黑衣人3 MIB星际战警3") == "黑衣人3 MIB星际战警3"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("镀金时代 豆瓣8 6", "镀金时代"),  # 不带「分」，靠 `豆瓣` 这个标记认
            ("杀她 KillHer IMDB 8 7", "杀她 KillHer"),
        ],
    )
    def test_marker_led_rating_without_the_unit_is_stripped(self, raw: str, expected: str) -> None:
        """`豆瓣` / `IMDB` 本身是足够强的标记，不必再要求「分」字兜底。"""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("早春晴朗 4KHQHDR60FPS", "早春晴朗"),  # 连写
            ("花开锦绣 10bi &FLAC无损HiFi声", "花开锦绣"),
            ("中情局律师 SDR&HDR", "中情局律师"),  # `&` 串起来的
            ("纽约失婴记 HDR&DV", "纽约失婴记"),
            ("樱桃新滋味 &Dv", "樱桃新滋味"),  # 只剩半边的 `&`
        ],
    )
    def test_av_spec_run_is_stripped(self, raw: str, expected: str) -> None:
        """`4KHQHDR` 不能靠逐个剥：摘走 `4K` 之后 `hq` 两边都是字母，`\\bhq\\b` 对不上。"""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize("title", ["DV时代", "HD世界", "4K先生"])
    def test_av_spec_run_needs_two_segments(self, title: str) -> None:
        """单独一段只能按 token 剔 —— 按子串剥会洗烂这些片名。"""
        assert clean_title(title) == title

    @pytest.mark.parametrize("title", ["Tom & Jerry", "王赫野 & 黄龄 过海"])
    def test_ampersand_between_real_words_is_kept(self, title: str) -> None:
        """光杆 `&` 不判噪声：《Tom & Jerry》里它是标题的一部分。

        两边都被剥空剩下的那个 `&`（`花开锦绣 &`）由末尾的 `strip` 收掉，
        那一步只动首尾，碰不到这里。
        """
        assert clean_title(title) == title


#: 幂等性用例。前一半是生产库 `repair scan` 实测吐出来的真实标题，
#: 后一半是各条「剥前缀」规则的正例 —— 它们必须照旧被剥掉，
#: 修幂等性不能把规则本身修没了。
_IDEMPOTENCE_CORPUS = (
    "E T 外星人 简 幕",
    "K Pop 猎魔女团 国日英多",
    "C 语言高级课程",
    "G I G N:精英部队",
    "大小：440.41MB",
    "长安三万里 IMAX Enhanced DTS UHD9 1",
    "周杰伦 太阳之子全专辑 MP3 附音乐播放器 foobar 椒盐音乐 官方MV 4K",
    "小森林:夏秋篇 REMUX",
    "唐伯虎點秋香2之四大才子 粤语",
    "007803 诛天大主宰",
    "text 诛天大主宰",
    "D 大主宰 动漫版",
    "L 狼的孩子雨和雪",
    "G 灌篮高手",
    "A计划",
    "K歌情人",
    "X战警 天启",
    "名称 大主宰 年番2",
    "电视剧：师兄太稳健 (2026)",
    "全民攻防:我有签到系统 作者:奏光 txt",
    "Some.Title.2024.1080p.WEB-DL.H265",
    # 这一批是 2026-10 那轮生产库噪声扫描新加的规则，每条都得盯幂等
    "某片 2001年剧情片",
    "1998年香港电影 喜剧片 整容日记",
    "年会不能停",
    "真心半解 8 0分",
    "莲花楼 豆瓣8 5分",
    "某纪录片 120分钟",
    "刀锋战士13部全",
    "西游记 第3部",
    "◎译 名 曼哈顿 曼克顿",
    "◎片 名 大话西游之月光宝盒 ◎年 代 1995 ◎产 地 中国香港",
    "误杀 简英",
    "中国 蓝盔",
    "No 027 唐伯虎点秋香",
    "金钱游戏 全",
    "小心许愿 硬",
    "种群 2G 集",
    "完美世界",
    "盗版时代",
    "雷霆扫毒 11 3GB",
    "黑衣人3 MIB星际战警3",
    "镀金时代 豆瓣8 6",
    "早春晴朗 4KHQHDR60FPS",
    "切尔诺贝利 DV&HDR 特效",
    "Tom & Jerry",
)


class TestCleanTitleIdempotent:
    """`clean_title` 必须是幂等的：洗过一遍的标题再洗一遍不能再变。

    这不是洁癖，是 `repair` 节点能不能无人值守跑的前提。
    `services/repair/plan.py` 每轮都拿库里存着的 `media.title`（**已经洗过的**）
    重算一遍 `clean_title`，不等于原值就落一条 `retitle` 任务。规则只要不幂等，
    每一轮都会检出同一批行、改一点、下一轮再改一点 —— 而
    `media.original_title` 全库为空，改掉就找不回来了。

    生产库实测踩到的两条（都已在 `_JUNK_PREFIX_RES` 里修掉）：

    * `E T 外星人` → `T 外星人` → `外星人`，每轮吃掉一个首字母。
    * `大小：440.41MB` → `440 41MB` → `41MB`，第二轮把 `440` 当行号剥了。
    """

    @pytest.mark.parametrize("title", _IDEMPOTENCE_CORPUS)
    def test_second_pass_changes_nothing(self, title: str) -> None:
        once = clean_title(title)
        assert clean_title(once) == once

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # 单字母列名后面跟汉字 —— 这才是这条规则要对付的东西，照旧剥掉
            ("D 大主宰 动漫版", "大主宰"),
            ("L 狼的孩子雨和雪", "狼的孩子雨和雪"),
            ("G 灌篮高手", "灌篮高手"),
            ("007803 诛天大主宰", "诛天大主宰"),
            # 单字母后面是拉丁词 —— 那是片名自己的一部分，不能动
            ("K Pop 猎魔女团", "K Pop 猎魔女团"),
            ("E T 外星人", "E T 外星人"),
            ("D Blade Runner", "D Blade Runner"),
        ],
    )
    def test_single_letter_prefix_only_strips_before_han(self, raw: str, expected: str) -> None:
        """区分「列名 + 中文片名」和「片名本身以单字母开头」只能靠后面是不是汉字。"""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "title",
        [
            "C 语言高级课程",
            "C 程序设计",
            "C 罗传奇",
            "A 计划",
            "X 战警 逆转未来",
            "K 歌情人",
            "O 记实录",
            "W 两个世界",
        ],
    )
    def test_whitelisted_letter_titles_keep_their_first_letter(self, title: str) -> None:
        """「后面是汉字」这道约束挡不住首字母真属于片名的那些，白名单兜住。

        实测 `C 语言高级课程` 被剥成了 `语言高级课程`。白名单按字母+词配对，
        所以放过的只是 `C 语言`，`D 语言` 照旧当列名残留剥掉（见下一条）。
        """
        assert clean_title(title) == title

    def test_whitelist_is_paired_to_the_letter(self) -> None:
        """白名单认的是「这个字母 + 这个词」，不是光认后面那个词。"""
        assert clean_title("D 语言高级课程") == "语言高级课程"

    def test_junk_titles_may_still_shrink(self) -> None:
        """垃圾标题不要求幂等 —— 它走的是 `delete`，不是 `retitle`。

        `repair/plan.py` 先判 `looks_like_junk_title(new_title)` 再判标题漂移，
        所以这类行一轮就被删掉，不会卷进"改一点、再改一点"的回路。
        这条用例把这个前提钉住：下面这个分享 ID 每一步都仍然判为垃圾。
        """
        raw = "1942762757558784044_aeWVVxu726g3waa-"
        once = clean_title(raw)
        assert clean_title(once) != once
        assert looks_like_junk_title(raw)
        assert looks_like_junk_title(once)
        assert looks_like_junk_title(clean_title(once))


class TestTagLineTitles:
    """Telegram 频道的「🏷 标签：」行被当成了作品。

    生产库里这一类 20 行 media 挂了 18863 条资源，其中 `#短剧` 一行就 12207 条
    —— 不是因为它出现了一万次，而是 `extract/rule.py` 的 `shared_links` 让
    每个段落都拿到全文档的链接，几千条消息累积到了同一个"作品"底下。
    """

    @pytest.mark.parametrize(
        "title",
        [
            "#短剧",
            "#电影",
            "#动漫",
            "#剧集",
            "#综艺",
            "#纪录片",
            # 裸的类目词（没有 `#`）同样不是作品名
            "短剧",
            "电影",
            "真人秀",
            "影视",
        ],
    )
    def test_a_lone_category_word_is_not_a_work(self, title: str) -> None:
        assert looks_like_junk_title(clean_title(title))

    @pytest.mark.parametrize(
        "title",
        [
            "#动漫 #短剧",
            "#电影 #动漫",
            "#动漫 #短剧 #综艺",
            "#电影 #纪录片",
            "#剧集 #短剧",
            # 模板原文整行，`clean_title` 会把 `🏷 标签：` 剥掉
            "🏷 标签：#短剧 #最新短剧 #热播短剧",
        ],
    )
    def test_a_string_of_category_tags_is_not_a_work(self, title: str) -> None:
        assert looks_like_junk_title(clean_title(title))

    @pytest.mark.parametrize(
        "title",
        [
            "#紧急呼救",
            "#训练日",
            "#Luimelia",
            "#零之使魔",
            "#21克",
            "#家人募集中",
            "#亲爱的小美人鱼",
            "#凡人修仙传 #年番4 #幕兰之战 #4K 11点以后",
            "#七王国的骑士# HBO #A Knight of the Seven Kingdoms#",
            "#怪奇物语# flix #Stranger Things#",
            "#斩神之凡尘神域 第二季 本季完",
        ],
    )
    def test_a_hashtag_prefixed_real_work_survives(self, title: str) -> None:
        """**这条是上面两条的约束条件，不是补充。**

        这些频道的片名本身就带 `#`，上面那些标题在库里都是真作品（`#紧急呼救`
        117 条资源、`#训练日` 95 条）。所以判据只能是「逐词都是类目词」，
        不能是「全是 hashtag」那个形态 —— 后者写起来短得多，也能把标签行全
        拦下，代价是连这十一行一起删掉，而删除不可逆。
        """
        assert not looks_like_junk_title(clean_title(title))

    def test_a_non_category_tag_string_is_deliberately_let_through(self) -> None:
        """`#VPN #SednaVPN` 是频道广告，但它漏过去 —— 这是有意的。

        想拦住它就得放弃「逐词都是类目词」这个判据，而那会连带误杀上面那批
        真作品。它在生产库里只有 4 条资源。
        """
        assert not looks_like_junk_title(clean_title("#VPN #SednaVPN"))

    @pytest.mark.parametrize("title", ["◎年 代", "感谢"])
    def test_site_template_leftovers(self, title: str) -> None:
        """影视站模板的字段名和频道的客套话，各挂着 964 / 683 条资源。"""
        assert looks_like_junk_title(clean_title(title))


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

    @pytest.mark.parametrize(
        "text",
        [
            "全民攻防:我有签到系统 作者:奏光 txt",
            "大主宰 我荒古圣体当为天帝 作者:墨之所想 txt",
            "某书 epub",
            "某轻小说合集",
        ],
    )
    def test_book_signals(self, text: str) -> None:
        """非影视资源要认出来 —— 它们默认不进搜索结果（见 `MediaType` docstring）。"""
        assert guess_media_type(text) == MediaType.BOOK

    def test_book_signal_beats_video_keyword(self) -> None:
        """小说正文常写「已改编动画」。先跑影视词表就会把一本书判成 anime。"""
        assert guess_media_type("某书 作者:张三 已改编动画 番剧") == MediaType.BOOK

    @pytest.mark.parametrize(
        ("text", "media_type"),
        [
            # 「作者」不带冒号 —— 是正文叙述，不是字段名
            ("《狄金森》1 3季 小妇人作者来串场", MediaType.UNKNOWN),
            # `词曲作者:` 是音乐署名，`原著作者:` 是影视的改编来源
            (": 词曲作者 Songwriter 年纪录片", MediaType.DOCUMENTARY),
            ("活着 原著作者:余华", MediaType.UNKNOWN),
            # `text` 是表格列名残渣（`_SHEET_MARKER_RE` 负责剥），不是 txt 电子书
            ("text 25 重获新生:母亲的逆袭", MediaType.UNKNOWN),
        ],
    )
    def test_book_false_positives(self, text: str, media_type: MediaType) -> None:
        """判成 book 等于从影视搜索里藏起来，所以宁可漏判不可误判。"""
        assert guess_media_type(text) == media_type
