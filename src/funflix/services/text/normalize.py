"""剧名归一与元信息提取。

全是确定性纯函数，不依赖数据库和网络 —— 这一层是整个流水线里最该被测透的部分：
`norm_key` 决定了两条资源会不会被合并到同一部作品下，错了会静默地污染数据。
"""

from __future__ import annotations

import re
import unicodedata

from funflix.base.enums import MediaType, Quality

# --- 噪声词表 ---------------------------------------------------------------

#: 画质 / 片源 / 编码 / 音轨。这些描述的是"这一份文件"，不是"这部作品"。
_QUALITY_TOKENS = (
    "2160p",
    "1080p",
    "1080i",
    "720p",
    "576p",
    "480p",
    "4k",
    "8k",
    "uhd",
    "fhd",
    "hd",
    "hdr10+",
    "hdr10",
    "hdr",
    "sdr",
    "dolbyvision",
    "dovi",
    "dv",
    "remux",
    "bluray",
    "blu-ray",
    "bdrip",
    "bdremux",
    "web-dl",
    "webdl",
    "webrip",
    "web",
    "hdtv",
    "dvdrip",
    "hdrip",
    "tvrip",
    "h264",
    "h265",
    "x264",
    "x265",
    "hevc",
    "avc",
    "av1",
    "10bit",
    "8bit",
    "aac",
    "ac3",
    "dts-hd",
    "dts",
    "truehd",
    "atmos",
    "flac",
    "ddp5.1",
    "dd5.1",
    "国语",
    "粤语",
    "英语",
    "日语",
    "韩语",
    "双语",
    "多语",
    "原声",
    "蓝光",
    "原盘",
    "高清",
    "超清",
    "标清",
    "高码",
    "高码率",
    "杜比视界",
    "杜比全景声",
)

#: 字幕相关
_SUBTITLE_TOKENS = (
    "中字",
    "中英字幕",
    "简繁",
    "简体",
    "繁体",
    "内嵌",
    "内封",
    "外挂",
    "官方中字",
    "无字幕",
    "生肉",
    "熟肉",
    "双字",
)

#: 版本 / 状态描述
_STATUS_TOKENS = (
    "完结",
    "已完结",
    "未删减",
    "删减版",
    "修复版",
    "重制版",
    "加长版",
    "导演剪辑版",
    "剧场版本",
    "抢先版",
    "枪版",
    "试看版",
)

#: 标题行的引导词，如 `名称：xxx`
_TITLE_MARKERS = (
    "名称",
    "片名",
    "剧名",
    "标题",
    "资源名称",
    "资源名",
    "影片名称",
    "影片名",
    "电影名",
    "剧集名",
    "番名",
    "title",
    "name",
)

_TITLE_PREFIX_RE = re.compile(
    rf"^\s*(?:\d+\s*[.、)）]\s*)?(?:{'|'.join(_TITLE_MARKERS)})\s*[:：]\s*",
    re.IGNORECASE,
)

#: 分辨率写法，如 1920x1080。必须在提取年份前剥掉，否则 1920 会被当成年份。
_RESOLUTION_RE = re.compile(r"\b\d{3,4}\s*[xX×]\s*\d{3,4}\b")

#: 完整日期。必须整体剥掉 —— 只抠走年份会把 `2026年8月25日` 留成 `年8月25日`。
_DATE_RE = re.compile(r"\d{4}\s*[年/.\-]\s*\d{1,2}\s*[月/.\-]\s*\d{1,2}\s*日?")

#: 分类标签。它们描述作品类别而非作品身份，留在归一键里会让
#: `电视剧：某剧` 和 `某剧` 变成两部不同的作品。
#: 只在「独立 token」或「行首带冒号」时剥，避免误伤《动画人生》这类标题。
_CATEGORY_TOKENS = (
    "电视剧",
    "电影",
    "动漫",
    "动画",
    "国漫",
    "日漫",
    "美漫",
    "番剧",
    "短剧",
    "微短剧",
    "剧集",
    "连续剧",
    "网剧",
    "国剧",
    "美剧",
    "韩剧",
    "日剧",
    "港剧",
    "台剧",
    "泰剧",
    "英剧",
    "综艺",
    "纪录片",
    "影片",
    "剧场版",
)

_TYPE_PREFIX_RE = re.compile(rf"^\s*(?:{'|'.join(_CATEGORY_TOKENS)})\s*[:：]\s*")

#: 已知的压制组 / 发布组署名。**这个列表必然不全** ——
#: 组名是开放集合，新组随时出现，只能持续补。
#: 不做通用规则（比如"剥掉末尾的英文 token"），那会误杀真正的外语片名。
_RELEASE_GROUP_TOKENS = (
    "hiveweb",
    "hive",
    "frds",
    "cmct",
    "beast",
    "wiki",
    "hdchina",
    "hdsky",
    "ourbits",
    "ttg",
    "mteam",
    "chd",
    "ourtv",
    "nukehd",
    "sublime",
)

#: 年份：1900-2099，且左右不能紧邻其它数字
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

_BRACKET_RE = re.compile(r"[\[【（(〔｛{][^\[\]【】（）()〔〕｛｝{}]{0,40}[\]】）)〕｝}]")

#: 各类表情与装饰符号
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0000fe00-\U0000fe0f\U00002190-\U000021ff]+"
)

#: 光杆季号（`S01`、`S2`）。它跟 `第N季` 是同一个东西 ——
#: **是作品身份，不是集数噪声**，所以单独拎出来，不参与标题清洗。
_BARE_SEASON_RE = re.compile(r"\bS\d{1,2}\b", re.I)

#: 第一季。视为隐含默认值，从标题里剥掉，见 `clean_title` 里的说明。
_SEASON_ONE_RE = re.compile(r"\bS0*1\b", re.I)

#: `S01E05` 这种「季+集」写法。清洗标题时只剥掉集号、**留下季号** ——
#: 整段剥掉的话，「某剧 S01E05」会洗成「某剧」，而「某剧 S01」洗成「某剧 S01」，
#: 同一季的两条分享反而成了两部作品。
_SEASON_EPISODE_RE = re.compile(
    r"\b(?P<season>S\d{1,2})\s*E\d{1,3}(?:\s*[-~]\s*E?\d{1,3})?\b", re.I
)

#: 从标题里剥掉的集数噪声。不含光杆季号，见 `_BARE_SEASON_RE`。
_TITLE_EPISODE_PATTERNS = (
    re.compile(r"全\s*\d+\s*[集话話期]"),
    re.compile(r"更新至\s*(?:第)?\s*\d+\s*[集话話期]?"),
    re.compile(r"第\s*\d+\s*[-~－]\s*\d+\s*[集话話期]"),
    re.compile(r"\bEP?\d{1,3}(?:\s*[-~]\s*EP?\d{1,3})?\b", re.I),
)

#: 识别集数信息时用的全集模式 —— 这里要认季+集与光杆季号，
#: 「能不能识别出来」和「要不要从标题里剥掉」是两件事。
_EPISODE_PATTERNS = (*_TITLE_EPISODE_PATTERNS, _SEASON_EPISODE_RE, _BARE_SEASON_RE)

#: 作品类型的判定关键词。命中越靠前的越优先。
_MEDIA_TYPE_KEYWORDS: tuple[tuple[MediaType, tuple[str, ...]], ...] = (
    (MediaType.VARIETY, ("综艺", "真人秀", "脱口秀", "访谈")),
    (MediaType.DOCUMENTARY, ("纪录片", "纪实", "documentary")),
    (MediaType.ANIME, ("动漫", "动画", "番剧", "国漫", "日漫", "anime")),
    (
        MediaType.TV,
        (
            "电视剧",
            "剧集",
            "连续剧",
            "网剧",
            "国剧",
            "短剧",
            "微短剧",
            "美剧",
            "韩剧",
            "日剧",
            "港剧",
            "台剧",
            "泰剧",
            "英剧",
        ),
    ),
    (MediaType.MOVIE, ("电影", "影片", "movie", "剧场版")),
)

#: 集数特征 → 剧集。在没有显式类型词时用。
#: 必须带 re.I：调用方会先把文本转小写，不加的话 `S01E01` 永远匹配不上。
_SERIES_HINT_RE = re.compile(
    r"全\s*\d+\s*[集话話期]|更新至|第\s*[一二三四五六七八九十\d]+\s*[季集]|\bS\d{1,2}(?:E\d|\b)",
    re.IGNORECASE,
)


# --- 标题清洗 ---------------------------------------------------------------


def strip_title_marker(line: str) -> str:
    """去掉 `名称：` 这类引导词，返回其后的内容。没有引导词则原样返回。"""
    return _TITLE_PREFIX_RE.sub("", line).strip()


def clean_title(raw: str) -> str:
    """把带噪声的标题洗成可展示的干净标题。

    刻意**保留** `第N季` / `第N部` —— 它们是作品身份的一部分，
    剥掉会把《某剧 第一季》和《某剧 第二季》错并成同一部。
    """
    text = unicodedata.normalize("NFKC", raw)
    text = strip_title_marker(text)
    text = _TYPE_PREFIX_RE.sub("", text)
    text = _EMOJI_RE.sub(" ", text)
    text = _RESOLUTION_RE.sub(" ", text)
    text = _DATE_RE.sub(" ", text)

    # 括号内容通常是画质/字幕/年份等注记，整体剥掉
    prev = None
    while prev != text:
        prev = text
        text = _BRACKET_RE.sub(" ", text)

    # 季+集写法只剥集号，季号按下面的规则处理（S01E05 → S01 → 再被剥掉）
    text = _SEASON_EPISODE_RE.sub(lambda m: f" {m.group('season')} ", text)
    # 第一季是**隐含的默认值**：绝大多数剧只有一季，`某剧 S01E01-E20` 与
    # `某剧 全20集` 说的是同一部，留着 S01 会把它们拆成两部 —— 而这种写法
    # 在真实语料里非常常见。S02 及以后才是真正区分作品身份的信息，予以保留。
    text = _SEASON_ONE_RE.sub(" ", text)

    # 注意用的是 _TITLE_EPISODE_PATTERNS：光杆季号（S01）不在里面。
    # 它和 `第一季` 一样属于作品身份，剥掉会把 S01 和 S02 归成同一部 ——
    # 中文写法一直是对的，英文写法曾经走的是被错并的那条路。
    for pattern in _TITLE_EPISODE_PATTERNS:
        text = pattern.sub(" ", text)

    # 点号/下划线分隔的发布名（Some.Title.2024.1080p）先还原成空格，再逐词剔噪声
    text = re.sub(r"[._]+", " ", text)

    # 只在「独立 token」层面剔噪声：`国漫`、`HiveWeb` 这类是被 / 或空格分开的独立项，
    # 而《动画人生》整体是一个 token，不会被误伤。
    noise = {
        t.lower()
        for t in (
            *_QUALITY_TOKENS,
            *_SUBTITLE_TOKENS,
            *_STATUS_TOKENS,
            *_CATEGORY_TOKENS,
            *_RELEASE_GROUP_TOKENS,
        )
    }
    tokens = [t for t in re.split(r"[\s|/\\]+", text) if t]
    kept = [t for t in tokens if t.lower().strip("-") not in noise]
    text = " ".join(kept)

    # 中文噪声词可能粘在其它字符上，逐个再扫一遍
    for token in (*_SUBTITLE_TOKENS, *_STATUS_TOKENS):
        text = text.replace(token, " ")

    text = _YEAR_RE.sub(" ", text)
    text = re.sub(r"[\s\-—–_]+", " ", text).strip(" -—–_、,，.。|/\\")
    return text.strip()


def norm_key(title: str) -> str:
    """作品归一键。

    用于 `(norm_key, media_type, year)` 唯一约束，决定两条资源是否指向同一部作品。
    在 clean_title 的基础上再抹掉全部空白与标点并转小写，
    让「误杀2」「误杀 2」「误杀Ⅱ」这类写法收敛到一起。
    """
    text = clean_title(title)
    text = unicodedata.normalize("NFKC", text).lower()
    text = _to_simplified(text)
    return "".join(ch for ch in text if ch.isalnum())


def _to_simplified(text: str) -> str:
    """繁体转简体。opencc 是可选依赖，缺失时原样返回。"""
    try:
        from opencc import OpenCC
    except ImportError:
        return text
    return OpenCC("t2s").convert(text)


# --- 元信息提取 -------------------------------------------------------------


def extract_year(text: str) -> int | None:
    """提取上映年份。同时出现多个时取第一个 —— 标题里的年份通常在最前面。

    先把**完整日期**剥掉再找年份。分享文案里「2025年8月25日更新」这类
    发帖日期极常见，不剥的话它会被当成上映年份 —— 而 `_upsert_media` 按
    `(norm_key, media_type, year)` 认作品，于是同一部片按发帖日期裂成好几个
    media，链接各分一半。`clean_title` 一直是剥日期的，这里漏了，两边对不上。
    """
    normalized = unicodedata.normalize("NFKC", text)
    cleaned = _DATE_RE.sub(" ", _RESOLUTION_RE.sub(" ", normalized))
    match = _YEAR_RE.search(cleaned)
    return int(match.group(1)) if match else None


#: 目录帖/合集帖的标题特征。命中即认为这条内容不代表某一部具体作品，
#: 而是"某天更新的一批"——把它当作品会在 media 表里堆出大量日期式假条目。
_CATALOG_TITLE_RE = re.compile(
    r"目录|合集|打包|合辑|片单|清单|资源包|更新列表|更新\s*\d+\s*部"
    r"|\d{1,2}\s*[月/\-]\s*\d{1,2}\s*[日号]"
)


def looks_like_catalog(title: str) -> bool:
    """标题是否像目录帖。"""
    return bool(title and _CATALOG_TITLE_RE.search(unicodedata.normalize("NFKC", title)))


#: 井号标签，Telegram 与论坛文案里最常见的分类信号
_HASHTAG_RE = re.compile(r"#([一-鿿A-Za-z0-9]{1,12})(?![一-鿿A-Za-z0-9])")

#: 已知的地区词，用于把标签归到 region 维度
_REGION_WORDS = frozenset(
    {
        "国产",
        "内地",
        "大陆",
        "香港",
        "台湾",
        "美国",
        "日本",
        "韩国",
        "英国",
        "法国",
        "泰国",
        "印度",
        "欧美",
        "日韩",
        "港台",
    }
)
_LANGUAGE_WORDS = frozenset({"国语", "粤语", "英语", "日语", "韩语", "闽南语", "方言"})

#: 这些井号标签是频道自宣或操作提示，不是作品分类
_TAG_STOPWORDS = frozenset(
    {
        "转存",
        "收藏",
        "分享",
        "更新",
        "资源",
        "网盘",
        "夸克",
        "阿里",
        "百度",
        "求片",
        "投稿",
        "频道",
        "群组",
        "广告",
    }
)


def tag_norm_key(name: str) -> str:
    """标签归一键。「科 幻」「科幻」应收敛到一起。"""
    text = unicodedata.normalize("NFKC", name).lower()
    return "".join(ch for ch in _to_simplified(text) if ch.isalnum())


#: 题材白名单。**只有在这张表里的井号标签才算题材。**
#:
#: 不用「井号标签一律当题材」是因为真实文案里作者会把剧名也打成标签
#: （`#吞噬星空`），当成题材会在筛选导航里堆出一堆只对应一部作品的假分类。
#: 表外的标签归到 other 维度：不丢弃、可查询，但不进题材导航。
_GENRE_WORDS = frozenset(
    {
        "剧情",
        "喜剧",
        "爱情",
        "动作",
        "悬疑",
        "惊悚",
        "恐怖",
        "犯罪",
        "推理",
        "科幻",
        "奇幻",
        "玄幻",
        "武侠",
        "仙侠",
        "古装",
        "历史",
        "战争",
        "军事",
        "青春",
        "校园",
        "家庭",
        "伦理",
        "职场",
        "都市",
        "农村",
        "年代",
        "谍战",
        "冒险",
        "灾难",
        "传记",
        "音乐",
        "歌舞",
        "励志",
        "治愈",
        "热血",
        "搞笑",
        "甜宠",
        "虐恋",
        "宫斗",
        "宅斗",
        "重生",
        "穿越",
        "系统",
        "无限流",
        "西幻",
        "末世",
        "赛博朋克",
        "公路",
        "文艺",
        "商战",
        "医疗",
        "刑侦",
        "反转",
        "群像",
        "单元剧",
        "催泪",
        "爽剧",
    }
)


def classify_tag(name: str) -> str:
    """判断标签属于哪个维度。返回 TagKind 的值。

    维度判定是保守的：认不出来就归 other，而不是默认塞进 genre。
    题材导航一旦被剧名污染就没法用了，而 other 里的标签随时可以再捞出来。
    """
    if name in _REGION_WORDS:
        return "region"
    if name in _LANGUAGE_WORDS:
        return "language"
    if re.fullmatch(r"(19|20)\d{2}|\d0年代", name):
        return "year"
    if name in _GENRE_WORDS:
        return "genre"
    return "other"


def extract_tags(text: str) -> list[tuple[str, str]]:
    """从文本里抽井号标签，返回 [(维度, 标签名)]，按出现顺序去重。

    只取井号标签而不做题材猜测 —— 分享文案里的 `#悬疑` 是作者的明确标注，
    可信；从简介里猜题材则会大量误标，污染筛选导航。
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for raw in _HASHTAG_RE.findall(unicodedata.normalize("NFKC", text)):
        name = raw.strip()
        key = tag_norm_key(name)
        if not key or key in seen or name in _TAG_STOPWORDS:
            continue
        seen.add(key)
        out.append((classify_tag(name), name))
    return out


def extract_category(text: str) -> str | None:
    """提取分类标签（`电视剧：` 前缀或独立 token），供类型判定使用。"""
    normalized = unicodedata.normalize("NFKC", strip_title_marker(text))
    prefix = _TYPE_PREFIX_RE.match(normalized)
    if prefix:
        return prefix.group(0).strip(" :：")
    tokens = re.split(r"[\s|/\\]+", normalized)
    return next((t for t in tokens if t in _CATEGORY_TOKENS), None)


def extract_quality(text: str) -> Quality:
    lowered = unicodedata.normalize("NFKC", text).lower()
    if re.search(r"\b(4k|2160p|8k)\b|4k|超清|蓝光原盘", lowered):
        return Quality.UHD_4K
    if re.search(r"\b(1080[pi]|fhd)\b", lowered):
        return Quality.FHD_1080P
    if re.search(r"\b720p\b", lowered):
        return Quality.HD_720P
    if re.search(r"\b(480p|576p|标清)\b", lowered):
        return Quality.SD
    return Quality.UNKNOWN


def extract_episode_info(text: str) -> str | None:
    """提取集数描述，如 `全40集` / `S01E01-E12`。取最先出现的一个。"""
    normalized = unicodedata.normalize("NFKC", text)
    best: tuple[int, str] | None = None
    for pattern in _EPISODE_PATTERNS:
        m = pattern.search(normalized)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(0).strip())
    return best[1] if best else None


def guess_media_type(text: str, title: str | None = None) -> MediaType:
    """按关键词猜作品类型，猜不出返回 UNKNOWN（不臆断为电影）。

    传了 `title` 就**优先看标题**：正文里的剧情简介经常顺带提到"动画""电影"，
    只看正文会把一部剧误判成动漫。标题给不出信号时才回落到正文。
    """
    if title:
        from_title = _match_media_type(title)
        if from_title is not MediaType.UNKNOWN:
            return from_title
    return _match_media_type(text)


def _match_media_type(text: str) -> MediaType:
    normalized = unicodedata.normalize("NFKC", text)
    lowered = normalized.lower()
    for media_type, keywords in _MEDIA_TYPE_KEYWORDS:
        if any(k in lowered for k in keywords):
            return media_type
    if _SERIES_HINT_RE.search(normalized):
        return MediaType.TV
    return MediaType.UNKNOWN
