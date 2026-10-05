"""剧名归一与元信息提取。

全是确定性纯函数，不依赖数据库和网络 —— 这一层是整个流水线里最该被测透的部分：
`norm_key` 决定了两条资源会不会被合并到同一部作品下，错了会静默地污染数据。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from funflix.base.enums import MediaType, Quality

MAX_TITLE_LENGTH = 500

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
    # 帧率与编码/音频参数。真实标题里是独立 token（`雁回时 贵女 Vivid 60FPS 首播04集`、
    # `速度与激情10 FastX iTunes DDP5 1 H265 DreamHD`），所以按 token 剔。
    "60fps",
    "30fps",
    "24fps",
    "fps",
    "ddp5",
    "ddp51",
    "ddp",
    # 流媒体片源平台。`nf` 要留在 token 层 —— 按子串剥会洗烂任何带 nf 的片名。
    "nf",
    "netflix",
    "itunes",
    "amzn",
    "disney",
    "hmax",
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
    # 连写的画质串。`4KHDR高码` 里的 `高码` 走子串剥之后剩下 `4KHDR`，
    # 它既不等于 `4k` 也不等于 `hdr`，不单独列出来就会留在归一键里
    # （生产库里这一个写法就单独裂出 73 行《大主宰》）。
    "4khdr",
    "4khdr10",
    "1080phdr",
    # 光杆分辨率数字。`1080p` 有，`1080` 没有 —— 而 `大主宰年番 1080 更26`
    # 这种写法在真实语料里不少。只在独立 token 层剔，所以不会碰到片名里的数字。
    "2160",
    "1080",
    "720",
    "480",
)

#: 粘在其它字符上的噪声，按**子串**剥（不要求是独立 token）。
#:
#: 跟 `_QUALITY_TOKENS` 的区别只在剥法：真实语料里 `4K高码`、`杜比音效中文字幕`、
#: `帧绮映画MAX` 是连写的，token 级剔除碰不到它们 —— 而 `大主宰 4K高码` 与
#: `大主宰 更新85集` 本来是同一部，不剥就是两行。
#:
#: 能进这张表的硬条件：**这个字符串不可能是任何片名的一部分**。
#: `hd` / `dv` / `web` 之类短词留在 `_QUALITY_TOKENS` 里按 token 剔 ——
#: 子串剥会把《HD世界》《DV时代》洗烂。
_GLUED_NOISE_TOKENS = (
    "杜比音效",
    # 生产库里就是这么写的（`WEB 4K 高码率 比音效 第5集`）——
    # 分享者自己把「杜」漏了。不认这个写法就会留下一个「比音效」。
    "比音效",
    "杜比环绕声",
    "杜比全景声",
    "杜比视界",
    "立体声",
    "帧绮映画",
    "绮映画",
    "hdr真彩",
    "真彩",
    "超分",
    "无水印",
    "新番首播",
    "定档pv",
    "链接不失效",
    "每周自动更新",
    "自动更新",
    "持续更新",
    "3d动画",
    "动漫版",
    "动画版",
    # 片源形态。`《美国狙击手》4K原盘REMUX`、`御赐小仵作 全4K` 整体是一个 token，
    # token 级剔除碰不到，必须按子串剥。
    "蓝光原盘",
    "原盘",
    "全4k",
    "全1080p",
    # 整条剥，不能指望后面的 `_SUBTITLE_TOKENS` —— 那一步只摘 `字幕`，
    # 留下一个孤零零的 `多国`（`大主宰年番2 IQ 杜比环绕声 多国字幕`
    # 就这么裂成了 `大主宰 IQ 中多国`）。
    "多国字幕",
    "多国语言",
    "多国配音",
    "国英双音",
    "双语音轨",
    "双音轨",
    "音轨",
    "双音",
)

#: 「地区 + 剧/片」复合词。来源站点的分类列这么写，而且**跟题材词连写**：
#: `资源的名称:古装大陆片 春家小姐是讼师` 里 `古装大陆片` 整体是一个 token，
#: token 级剔除碰不到它 —— 必须按子串剥，剥完剩下的 `古装` 才轮到题材词表。
#:
#: 只收**复合**形态，不收光杆地区词：`大陆片` 不可能是片名的一部分，
#: 而光杆 `美国` 按子串剥会把《美国狙击手》洗成《狙击手》。
_REGION_TOKENS = (
    "国产",
    "大陆",
    "内地",
    "香港",
    "台湾",
    "日本",
    "韩国",
    "美国",
    "英国",
    "泰国",
    "印度",
    "欧美",
    "日韩",
    "国漫",
    "日漫",
    "美漫",
)

#: 码率串单独用一条正则，**不能**拆成 `4k高码` / `高码率` / `高码` 几个字面量
#: 丢进上面那张表。正则的选择分支是「在字符串里出现得早的赢」，不是「最长的赢」：
#: 对 `4K高码率版`，`4k高码` 在位置 0 就命中，`高码率` 根本轮不到，剥完留下一个
#: 孤零零的 `率版` —— 生产库里 `大主宰年番 4K高码率版` 就这么裂成了独立作品
#: （`大主宰 率版`）。写成一条带可选部分的正则，贪婪匹配自然吃掉整串。
#:
#: 结尾的 `版` 跟着码率串一起剥：`高码率版` 是一个意思。单独的 `版` 不在这里
#: 处理（会洗烂《盗版时代》），交给 token 级剔除。
_BITRATE_RE = r"(?:4k)?[高中低]码(?:率)?(?:版)?"

_GLUED_NOISE_RE = re.compile(
    "|".join(
        [
            # 字面量按长度倒序，长的优先：同样位置起手时 `杜比环绕声` 不该被
            # 更短的分支截断。
            *(
                re.escape(t)
                for t in sorted(
                    (
                        *_GLUED_NOISE_TOKENS,
                        *(r + suffix for r in _REGION_TOKENS for suffix in ("剧", "片")),
                    ),
                    key=len,
                    reverse=True,
                )
            ),
            _BITRATE_RE,
        ]
    ),
    re.IGNORECASE,
)

#: 字幕相关。注意这张表在 `clean_title` 里**还会再按子串扫一遍**
#: （中文噪声词常粘在别的字上），所以别往里放可能是片名一部分的词。
_SUBTITLE_TOKENS = (
    "中字",
    "中文字幕",
    "英文字幕",
    "双语字幕",
    "官方字幕",
    "简体中文",
    "繁体中文",
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
    "嵌入",
    # 光杆「字幕」**必须排在最后**：这张表是按顺序逐个 `str.replace` 扫的，
    # 放前面会把 `中文字幕` 先啃成一个孤零零的 `中文`。
    "字幕",
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
    "首播",
    "全集",
)

#: 标题行的引导词，如 `名称：xxx`
_TITLE_MARKERS = (
    "名称",
    "片名",
    "剧名",
    "标题",
    "资源名称",
    "资源名",
    "资源标题",
    "资源的名称",
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

#: 题材标签。采集层把来源站点的「类型」列原样串进标题，于是
#: `大主宰 年番2 动作 奇幻 4KHDR高码` 的归一键里带上了题材
#: （生产库里这一个写法单独裂出 73 行）。题材描述的是作品属性，不是身份 ——
#: 同一部剧在不同站点被打上不同题材标签，不该因此裂成两部。
#:
#: **不能并进 `_CATEGORY_TOKENS`**：`extract_category` 取的是第一个命中的
#: token，题材混进去就会遮住真正的分类（`动作 电视剧 某剧` 会被判成「动作」）。
#:
#: 只在独立 token 层剔。《喜剧之王》《恐怖游轮》整体是一个 token，碰不到；
#: 代价是片名恰好就是单个题材词的作品（《爱情》）会被洗空 —— 这种片极少，
#: 换掉几万行假作品值得。
_GENRE_TOKENS = (
    "动作",
    "奇幻",
    "玄幻",
    "仙侠",
    "武侠",
    "喜剧",
    "爱情",
    "科幻",
    "悬疑",
    "恐怖",
    "惊悚",
    "战争",
    "犯罪",
    "冒险",
    "古装",
    "都市",
    "校园",
    "热血",
    "治愈",
    "后宫",
    "搞笑",
    "励志",
    "家庭",
    "青春",
    "历史",
    "军事",
    "传记",
    "歌舞",
    "剧情",
)

#: 分享文案里的推广词。**只在独立 token 层剔，不按子串剥** ——
#: 《天下没有免费的午餐》里的「免费」是片名的一部分。
_PROMO_TOKENS = (
    "免费",
    "免费观看",
    "在线观看",
    "免费看",
    "会员",
    "秒播",
    "直链",
    "资源分享",
    "资源合集",
    "合集",
    "打包",
    "全集",
)

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

#: 只在「独立 token」层面剔掉的噪声总表：`国漫`、`HiveWeb` 这类是被空格或 `/`
#: 分开的独立项，而《动画人生》整体是一个 token，不会被误伤。
#:
#: 在模块级建好而不是每次 `clean_title` 里重建 —— 归一重算要跑 90 万行，
#: 每行重新 lower() 并哈希两百多个词纯属白烧。
#:
#: 题材词额外派生 `X剧` / `X片` 两种后缀形态（`仙侠剧`、`动作片`）：
#: 来源站点的类型列这么写的很多，而列出全部组合会让词表翻三倍还容易漏。
#:
#: 还要派生带 `年` 前缀的形态（`年传记片`、`年剧情片`）。来源站点的模板是
#: 「片名 + 外文名 + 年份 + 题材片」，而**年份字段经常是空的**，于是
#: `: 旋风九日 年传记片` 这样的标题里留下一个光杆「年」粘在题材词上。
#: 不派生的话这一个写法就让每部片单独裂一行。
_TOKEN_NOISE: frozenset[str] = frozenset(
    t.lower()
    for t in (
        *_QUALITY_TOKENS,
        *_SUBTITLE_TOKENS,
        *_STATUS_TOKENS,
        *_CATEGORY_TOKENS,
        *_GENRE_TOKENS,
        *(g + suffix for g in _GENRE_TOKENS for suffix in ("剧", "片")),
        *(
            prefix + g + suffix
            for g in _GENRE_TOKENS
            for suffix in ("", "剧", "片")
            for prefix in ("年",)
        ),
        *(r + suffix for r in _REGION_TOKENS for suffix in ("剧", "片")),
        *_PROMO_TOKENS,
        *_RELEASE_GROUP_TOKENS,
        # 采集层把表格的列名当数据采了进来。`text 25 重获新生:母亲的逆袭` /
        # `三小姐太野 贾琳&杨建壮 text` —— 首尾都出现过，所以按 token 全局剔，
        # 不能只当前缀处理。整条标题就叫 `text` 的作品不存在。
        "text",
        "txt",
    )
)

#: 年份：1900-2099，且左右不能紧邻其它数字
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

_BRACKET_RE = re.compile(r"[\[【（(〔｛{][^\[\]【】（）()〔〕｛｝{}]{0,40}[\]】）)〕｝}]")

#: 书名号 / 直角引号。**只拆掉符号本身，不动里面的内容** ——
#: 跟 `_BRACKET_RE` 里那些方括号圆括号相反，这几种符号包的恰恰是片名：
#: `国漫《大主宰》更至03`、`「大主宰II」82 4K高码`。整段剥掉就把片名也剥了。
#:
#: 换成空格而不是直接删，是为了让贴着符号的噪声变成**独立 token** ——
#: `国漫《大主宰》` 整体是一个 token，token 级的分类词剔除碰不到它；
#: 拆成 `国漫 大主宰` 之后 `国漫` 才会被 `_CATEGORY_TOKENS` 摘掉。
_TITLE_QUOTE_RE = re.compile(r"[《》〈〉「」『』]")

#: 零宽字符、双向控制符、BOM、软连字符。看不见，但会让两个"一模一样"的标题
#: 算出不同的键 —— 生产库里 `大主宰 年番2\u200e` 和 `大主宰 年番2` 就是两行，
#: 肉眼看完全一致，不知道有这回事根本查不出来。NFKC 不负责删这些。
#:
#: 写成转义形式而不是贴字面量：字面量在编辑器里就是一片空白，
#: 谁都没法判断这个字符类到底包了哪些字符。
_INVISIBLE_RE = re.compile(
    "["
    "\u200b-\u200f"  # 零宽空格/连字符/非连字符 + LRM/RLM
    "\u202a-\u202e"  # 双向嵌入与覆写
    "\u2060-\u2064"  # word joiner 及不可见运算符
    "\u00ad"  # 软连字符
    "\ufeff"  # BOM / 零宽不换行空格
    "]"
)

#: 各类表情与装饰符号
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0000fe00-\U0000fe0f\U00002190-\U000021ff]+"
)

#: 光杆季号（`S01`、`S2`）。它跟 `第N季` 是同一个东西 ——
#: **是作品身份，不是集数噪声**，所以单独拎出来，不参与标题清洗。
#:
#: 边界用「左右不是拉丁字母/数字」而不是 `\b`：Python 的 `\w` 认汉字，
#: 用 `\b` 的话 `更新至S01E93`、`大主宰S02` 里的季号因为紧贴汉字而没有词边界，
#: 永远匹配不上 —— 而「汉字紧贴季号」在中文分享标题里是最常见的写法。
_LATIN_L = r"(?<![0-9A-Za-z])"
_LATIN_R = r"(?![0-9A-Za-z])"
_BARE_SEASON_RE = re.compile(rf"{_LATIN_L}S\d{{1,2}}{_LATIN_R}", re.I)

#: 第一季。视为隐含默认值，从标题里剥掉，见 `clean_title` 里的说明。
_SEASON_ONE_RE = re.compile(rf"{_LATIN_L}S0*1{_LATIN_R}", re.I)

#: `S01E05` 这种「季+集」写法。清洗标题时只剥掉集号、**留下季号** ——
#: 整段剥掉的话，「某剧 S01E05」会洗成「某剧」，而「某剧 S01」洗成「某剧 S01」，
#: 同一季的两条分享反而成了两部作品。
_SEASON_EPISODE_RE = re.compile(
    rf"{_LATIN_L}(?P<season>S\d{{1,2}})\s*E\d{{1,3}}(?:\s*[-~]\s*E?\d{{1,3}})?{_LATIN_R}", re.I
)

#: 从标题里剥掉的集数噪声。不含光杆季号，见 `_BARE_SEASON_RE`。
#:
#: 顺序有意义：带「全」「第」的写法排在光杆写法前面，否则 `全40集` 会被
#: 光杆的 `\d+集` 吃掉 `40集`、剩一个孤零零的「全」，`12集全` 同理。
_TITLE_EPISODE_PATTERNS = (
    re.compile(r"全\s*\d+\s*[集话話期]"),
    re.compile(r"\d+\s*[集话話期]\s*全"),
    re.compile(r"第\s*\d+\s*[-~－]\s*\d+\s*[集话話期]"),
    # `更新至93` / `更至03` / `首更03集` / `已更20` / `更新中` / `每周自动更新`
    # 是同一件事的不同写法。分享频道每更新一集就重发一条，标题只有这个数字在变
    # —— 不剥的话一部剧会按集数裂成几十行（实测《大主宰》一家就有 60+ 行是这么来的）。
    #
    # 「更」后面必须跟「新」或「至」，不接受光杆的「更」——
    # 否则《更上一层楼》《变更》这类片名会被咬掉一个字。
    re.compile(
        r"(?:每周)?\s*(?:自动|持续)?\s*(?:首|已|现|新)?\s*更(?:新|至)+\s*(?:第)?\s*\d*\s*[集话話期]?\s*(?:中)?"
    ),
    # 光杆的「更20」「更144」：要求紧跟数字，同样是为了不误伤含「更」的片名。
    re.compile(r"(?:首|已|现|新)?\s*更\s*(?:第)?\s*\d+\s*[集话話期]?"),
    # `含 版本` / `附第1季` —— 这是"这一份打包里还附带什么"的说明，
    # 不是作品身份。`大主宰 第2季 附第1季` 讲的仍然是第 2 季。
    re.compile(r"含\s*版本"),
    re.compile(r"附\s*第\s*(?:[一二三四五六七八九十]|\d{1,2})\s*[季部]"),
    re.compile(r"第\s*\d+\s*[集话話期]"),
    re.compile(r"\d+\s*[集话話期]"),
    re.compile(r"\bEP?\d{1,3}(?:\s*[-~]\s*EP?\d{1,3})?\b", re.I),
    re.compile(r"\d+\s*帧"),
)

#: 采集层把页面字段名/字段值串起来喂进抽取器时留下的标签残渣，
#: 如 `资源标题大主宰动作奇幻`、`描述大主宰大主宰导演马建平`、`标签大主宰动画动作`。
#: 这些是**抓取产物**而不是片名的一部分，留着会让同一部作品按来源站点裂开。
#: 字段名分两类剥，**判据是「这个词有没有可能是片名的一部分」**：
#:
#: - 长词组不可能出现在片名里，无条件剥。
#: - 短字段名必须**带冒号**才剥。不加这个限制就会出现
#:   `导演万岁` → `万岁`、`还要哄着大小姐` → `还要哄着姐`（`大小` 被当字段名吃掉）
#:   这种把片名正文剥烂的情况 —— 这两条都是生产库里的真实标题。
_SCRAPE_LABEL_RE = re.compile(
    r"资源的?名称|资源标题|影片名称|剧情简介|内容简介|豆瓣评分|文件大小"
    r"|提取码|访问码|分享码"
    r"|(?:简介|描述|标签|评分|大小|格式|时长|地区|主演|导演|编剧|上映|片长)\s*[:：]"
)

#: 整个豆瓣页面被当成标题采了进来。生产库里真实存在这种行：
#:
#:     描述:狼的孩子雨和雪 おおかみこどもの雨と雪 おおかみこどもの雨と雪导演: 细田守
#:     编剧: 奥寺佐渡子...主演: 宫崎葵 大泽隆夫...类型: 剧情 家庭 奇幻制片国家
#:     地区: 日本语言: 日语...又名: 狼之子雨与雪...的剧情简介 · · · 在某国立大学念书的花...
#:
#: `_SCRAPE_LABEL_RE` 只把标签词本身删掉、正文全留着，于是清洗完还有五百多字，
#: 每一行都算出一个独一无二的键 —— 这是长尾单行组里占比最大的一类。
#:
#: 这些字段名后面**必然跟冒号**，所以要求冒号就足够精确：
#: 《导演万岁》这种片名里的「导演」不带冒号，不会被误切。
#: `的剧情简介` 是豆瓣正文的固定起头，不带冒号，单独列出。
_SCRAPE_CUT_RE = re.compile(
    r"(?:导演|编剧|主演|配音|监制|出品|类型|制片国家|制片地区|语言|上映日期|首播日期"
    r"|首播|集数|单集片长|片长|又名|原名|豆瓣评分|imdb|剧情简介|内容简介|剧情介绍)"
    r"\s*[:：]"
    r"|的剧情简介"
)

#: 某个表格源的列名/行号被当成数据采了进来，真实形态长这样：
#:
#:     #000000 28 text 什么叫我都元婴期了还要哄着大小姐第二季
#:     text 25 重获新生:母亲的逆袭 鞠瑾&罗大雪
#:     侯门老祖宗重整家族荣耀 张萓紘&朱诗妤 text
#:
#: `000000` 和 `text` 是列名残渣，`28` / `25` 是行号，`鞠瑾&罗大雪` 是演员列。
#: 三样都出现在首尾任意位置。
_SHEET_MARKER_RE = re.compile(r"(?<![0-9a-z])(?:te?xt|0{4,}\d*)(?![0-9a-z])", re.I)

#: 独立成 token 的 1~3 位数字 —— 在表格源里是行号。
#: **只在确认是表格源之后才敢剥**：`大主宰 2` 里的 `2` 是季号，不是行号。
_SHEET_ROW_NO_RE = re.compile(r"(?<!\S)\d{1,3}(?!\S)")

#: 结尾的演员列（`诱引 刘擎&白妍`、`张萓紘&朱诗妤`）。要求用 `&` 连接两个以上
#: 2~4 字的中文名。抽样 25 条全部是真的演员列或分类/字幕列
#: （`银魂番剧&漫画`、`简英 & 繁英双语` 剥掉同样是对的），所以不限表格源。
#:
#: 开头的 `(?:^|\s)` 是**必须的安全闸**：没有它的话 `泰坦尼克号&阿凡达`
#: 会从中间咬住 `坦尼克号&阿凡达`，剥完只剩一个「泰」。有了它，
#: 第一个名字必须自己就是一个完整 token 才算。
#:
#: 单个结尾人名剥不了 —— 《我和我的祖国》这类片名的结尾本来就长得像人名，
#: 没有 `&` 就没有足够的信号。
_CAST_AMP_RE = re.compile(r"(?:^|\s)[一-鿿]{2,4}(?:\s*&\s*[一-鿿]{2,4})+\s*$")

#: 采集层把同一个标题重复串了两遍（`描述:少年江湖 少年江湖导演:...`），
#: 截断元信息之后留下 `少年江湖 少年江湖`。
#:
#: 重复单元至少 3 个字符，**而且**至少 3 个不同字符（见 `_REPEAT_MIN_DISTINCT`）。
#: 两个条件合起来就要求单元是三个互不相同的字，《妈妈》《很好很好》
#: 《高高兴兴》这类正常叠词都落不进来，而 `大主宰大主宰` 收得住 ——
#: 生产库里光这一个写法就单独裂出一组。
_REPEAT_RE = re.compile(r"(.{3,}?)\s*\1")

#: 重复单元里至少要有这么多个**不同**字符。没有这一条的话 `aaaa|aaaa`
#: 也算重复，`"a" * 600` 会被一路折叠成十来个字符。
_REPEAT_MIN_DISTINCT = 3


def _dedupe_repeat(text: str) -> str:
    """折叠紧挨着的重复串（`少年江湖 少年江湖` → `少年江湖`）。见 `_REPEAT_RE`。"""

    def collapse(match: re.Match[str]) -> str:
        unit = match.group(1)
        if len(set(unit.strip())) < _REPEAT_MIN_DISTINCT:
            # 单元是同一个字符重复（`aaaa`）或只有两种字符（`abab`），
            # 这不是采集重复而是正常文本，原样留下。
            return match.group(0)
        return unit

    # 反复折叠到不动点：`re.sub` 替换完是接着往后扫的，不会回头重新检查
    # 刚产出的结果，所以 `某某某某某某某某` 这类多重重复要靠外层循环收干。
    for _ in range(4):
        folded = _REPEAT_RE.sub(collapse, text)
        if folded == text:
            break
        text = folded
    return text


#: 漏进标题的 URL 碎片。`clean_title` 会把点号换成空格，于是
#: `aliyundrive.com/s/adg4qmqWc8j` 会碎成 `com s adg4qmqWc8j` 跟在片名后面 ——
#: 所以既要认完整 URL，也要认这种碎掉之后的形态。
_URL_FRAGMENT_RE = re.compile(
    r"https?\S*|magnet:\S*|\bwww[\s.]+\S+|\b[a-z0-9-]{2,}[\s.]+(?:com|cn|net|org)\b"
    # 域名后面跟着的路径段（`com s adg4qmqWc8j` 里的 `s`）。限定 1~3 个字符，
    # 不然会吃掉紧跟在 URL 后面的真实片名。
    r"|\b(?:com|cn|net|org)(?:[\s.]+[a-z0-9]{1,3}\b)*"
    r"|\bpan\s+\w+|\b[a-z]{2,}\d[a-z0-9]{6,}\b",
    re.IGNORECASE,
)

#: 标题最前面的垃圾前缀：表格里的行号/编号列（`007803 诛天大主宰`）、
#: 没带冒号所以 `_TITLE_PREFIX_RE` 没认出来的引导词（`名称 大主宰 年番2`）、
#: 以及采集层遗留的单字母/`text` 列名（`D 大主宰 动漫版`、`text 诛天大主宰`）。
_JUNK_PREFIX_RES = (
    re.compile(r"^\s*\d{3,8}\s+"),
    re.compile(rf"^\s*(?:{'|'.join(_TITLE_MARKERS)})\s+", re.IGNORECASE),
    # 表格残留的单字母列名：`D 大主宰 动漫版`、`L 狼的孩子雨和雪`、`G 灌篮高手`。
    # **必须跟空格**才剥 —— 《A计划》《K歌情人》《O记实录》里的首字母是
    # 紧贴片名的，不带空格，不会落进这一条。
    re.compile(r"^\s*[a-z]\s+", re.IGNORECASE),
)

#: 识别集数信息时用的全集模式 —— 这里要认季+集与光杆季号，
#: 「能不能识别出来」和「要不要从标题里剥掉」是两件事。
_EPISODE_PATTERNS = (*_TITLE_EPISODE_PATTERNS, _SEASON_EPISODE_RE, _BARE_SEASON_RE)

_CN_SEASON_EPISODE_RE = re.compile(
    r"第\s*[一二三四五六七八九十百零\d]+\s*季"
    r"(?:\s*第?\s*[一二三四五六七八九十百零\d]+(?:\s*[-~－]\s*\d+)?\s*集)?"
)

_SIZE_RE = re.compile(
    r"(?<![\w.])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT](?:I?B)?)\b",
    re.IGNORECASE,
)

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


def _strip_sheet_artifacts(text: str) -> str:
    """剥掉表格源的列名残渣与行号。见 `_SHEET_MARKER_RE`。

    **只在确认是表格源之后才剥行号** —— 判据是标题里出现了 `text` /
    `000000` 这类列名残渣。没有这个前提，`大主宰 2` 的 `2` 会被当成行号
    剥掉，而它其实是季号。
    """
    if not _SHEET_MARKER_RE.search(text):
        return text
    text = _SHEET_MARKER_RE.sub(" ", text)
    text = _SHEET_ROW_NO_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_cast_column(text: str) -> str:
    """剥掉结尾的演员列。见 `_CAST_AMP_RE`。

    剥完不能剩空串 —— 整条就只有演员名的行确实存在，那种情况原样退回，
    交给 `looks_like_junk_title` 去判。
    """
    return _CAST_AMP_RE.sub("", text).strip() or text


def _cut_scrape_dump(text: str) -> str:
    """整页元信息被采成标题时，从第一个字段名处截断。见 `_SCRAPE_CUT_RE`。

    切点在最前面（前面没有像片名的内容）时**不截断** —— 那种行整条都是
    元信息，截了会变成空串，留给 `looks_like_junk_title` 判垃圾更合适。
    """
    match = _SCRAPE_CUT_RE.search(text)
    if match is None or match.start() < 2:
        return text
    return text[: match.start()]


def clean_title(raw: str) -> str:
    """把带噪声的标题洗成可展示的干净标题。

    刻意**保留** `第N季` / `第N部` —— 它们是作品身份的一部分，
    剥掉会把《某剧 第一季》和《某剧 第二季》错并成同一部。
    """
    text = unicodedata.normalize("NFKC", raw)
    # 零宽字符与双向控制符要在最前面清掉。它们**完全不可见**，却实打实地
    # 进了归一键：生产库里 `大主宰 年番2‎` 和 `大主宰 年番2` 就是两行，
    # 肉眼看完全一样，怎么查都查不出为什么没合并。NFKC 不负责删这些。
    text = _INVISIBLE_RE.sub("", text)
    text = strip_title_marker(text)
    for pattern in _JUNK_PREFIX_RES:
        text = pattern.sub(" ", text)
    text = _TYPE_PREFIX_RE.sub("", text)
    text = _EMOJI_RE.sub(" ", text)
    # URL 要在换掉点号之前剥 —— 之后 `a.com/s/xxx` 就碎成一串假 token 了
    text = _URL_FRAGMENT_RE.sub(" ", text)
    text = _RESOLUTION_RE.sub(" ", text)
    text = _DATE_RE.sub(" ", text)
    # 整页元信息要**截断**而不是逐个删标签 —— 见 `_SCRAPE_CUT_RE`。
    # 必须排在 `_SCRAPE_LABEL_RE` 前面：后者会把 `导演` 这些切点词先删掉，
    # 删完就再也找不到从哪里截了。
    text = _cut_scrape_dump(text)
    text = _SCRAPE_LABEL_RE.sub(" ", text)

    # 括号内容通常是画质/字幕/年份等注记，整体剥掉
    prev = None
    while prev != text:
        prev = text
        text = _BRACKET_RE.sub(" ", text)

    # 书名号/直角引号反过来：只拆符号、留内容，见 `_TITLE_QUOTE_RE`
    text = _TITLE_QUOTE_RE.sub(" ", text)

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

    # 连写的噪声按子串剥，要在 token 级剔除之前 —— `大主宰 4K高码` 整体是
    # 一个 token，先剥成 `大主宰` 才轮得到后面的规则。见 `_GLUED_NOISE_TOKENS`。
    text = _GLUED_NOISE_RE.sub(" ", text)

    # 表格源的列名/行号要在分词前剥 —— 它依赖 token 边界，见 `_SHEET_MARKER_RE`
    text = _strip_sheet_artifacts(text)
    # 演员列必须排在上一步**之后**：`侯门老祖宗重整家族荣耀 张萓紘&朱诗妤 text`
    # 里的列名残渣挡在结尾，不先摘掉，锚在 `$` 上的演员列正则就对不上。
    text = _strip_cast_column(text)

    # 切分符里带 `+`：`中码率+4K` 这种用加号连写的画质串很常见，
    # 不切开的话 `+4K` 整体不等于 `4k`，token 级剔除就漏掉了。
    # （`4k` 不能进 `_GLUED_NOISE_TOKENS` 按子串剥 —— 会洗烂《4K先生》之类片名。）
    #
    # `丨` 是分享者拿来当竖线用的装饰符（`大主宰丨丨1080p丨`），但它是**汉字**
    # （U+4E28「丨」部），既不是标点也不是空白，不显式列出来就永远切不开。
    tokens = [t for t in re.split(r"[\s|丨/\\+]+", text) if t]
    kept = [t for t in tokens if t.lower().strip("-+") not in _TOKEN_NOISE]
    text = " ".join(kept)

    # 中文噪声词可能粘在其它字符上，逐个再扫一遍
    for token in (*_SUBTITLE_TOKENS, *_STATUS_TOKENS):
        text = text.replace(token, " ")

    text = _YEAR_RE.sub(" ", text)
    text = re.sub(r"[\s\-—–_]+", " ", text).strip(" -—–_、,，.。|/\\")
    # 重复串接的标题在这里才收得干净：前面各步剥完噪声，两遍标题之间
    # 残留的空格也已经归一成单个空格，重复才对得上。
    text = _dedupe_repeat(text).strip()
    # 冒号要在最后剥：`描述:雁回时` 里的 `描述` 被 `_SCRAPE_LABEL_RE` 删掉之后
    # 留下一个光杆 `:雁回时`，而冒号在前面几步里一直是字段分隔符，不能提前删。
    text = text.strip(" -—–_、,，.。|/\\:：")
    return text.strip()[:MAX_TITLE_LENGTH].rstrip()


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
    # `更新N部` 与省掉「新」字的 `更144部` 都要认 —— 后者在真实语料里同样常见，
    # 漏掉的话规则抽取器会把「更144部」当成剧名建进 media 表。
    r"目录|合集|打包|合辑|片单|清单|资源包|更新列表|更新?\s*\d+\s*部"
    r"|\d{1,2}\s*[月/\-]\s*\d{1,2}\s*[日号]"
)


def looks_like_catalog(title: str) -> bool:
    """标题是否像目录帖。"""
    return bool(title and _CATALOG_TITLE_RE.search(unicodedata.normalize("NFKC", title)))


#: 整条标题就是这些词之一时，它不是作品 —— 是被当成标题抓下来的页面文案。
#:
#: 这类行在库里的量级大到会改变产品观感：`夸克` 45888 行、`链接` 10771 行、
#: `查看资源` 6177 行、`磁力下载` 5636 行。它们各自还会挂上真实的网盘链接，
#: 于是搜索结果里出现一堆叫「夸克」的"作品"。
#:
#: 判定刻意只认**整条命中**，不做子串匹配 —— 《大小多少》《提取码》这类片名
#: 虽然罕见但确实可能存在，而误删是不可逆的。
_JUNK_TITLE_KEYS = frozenset(
    {
        # 网盘名
        "夸克",
        "夸克网盘",
        "夸克度盘",
        "百度",
        "百度盘",
        "百度网盘",
        "度盘",
        "阿里",
        "阿里云盘",
        "阿里网盘",
        "迅雷",
        "迅雷网盘",
        "天翼",
        "天翼云盘",
        "uc",
        "uc网盘",
        "115",
        "115网盘",
        "移动云盘",
        "光鸭云盘",
        "网盘",
        "云盘",
        # 链接/按钮文案
        "链接",
        "原链接",
        "下载链接",
        "分享链接",
        "社区链接",
        "网盘链接",
        "磁力",
        "磁力链接",
        "磁力下载",
        "下载",
        "在线播放",
        "立即播放",
        "查看资源",
        "查看详情",
        "点击查看",
        "点击",
        "浏览",
        "浏览器",
        "更多",
        "展开",
        "转存",
        "失效补档",
        "补档",
        "求片",
        "投稿",
        # 字段名
        "资源",
        "资源名",
        "资源名称",
        "标题",
        "名称",
        "描述",
        "简介",
        "标签",
        "备注",
        "序号",
        "详情",
        "大小",
        "文件大小",
        "提取码",
        "访问码",
        "分享码",
        "豆瓣评分",
        "评分",
        "格式",
        "时长",
        "地区",
        "年份",
        "类型",
        "画质",
        "集数",
        "更新",
    }
)

#: 网盘分享 ID 的形态：纯小写字母数字、够长、且**字母数字混有**。
#: `adg4qmqWc8j` 这种会漏进标题（见 `_URL_FRAGMENT_RE` 的说明）。
#: 要求混有数字是为了不误杀 `leoziyuan`、`oppenheimer` 这类纯字母的真实片名。
_SHARE_ID_KEY_RE = re.compile(r"^(?=[0-9a-z]*\d)(?=[0-9a-z]*[a-z])[0-9a-z]{10,}$")

#: 整条磁力链被当成了标题。生产库里这一类有 4000+ 行，
#: 其中最大的两组各有 3176 / 862 行 —— 因为 tracker 参数串完全一样，
#: 归一键撞在一起，看起来像"同一部作品"，实际上什么都不是。
#:
#: 为什么不靠 `_SHARE_ID_KEY_RE` 兜：那条规则要求键是**纯**小写字母数字，
#: 而磁力链里混着 percent-encoding 过的中文（`%8f%8c`），解出来有汉字，
#: 字符类不匹配就漏过去了。得按结构认，不能按字符组成认。
#:
#: 只收**不可能出现在片名里**的结构信号。`tracker` / `announce` 单独出现
#: 不算 —— 2010 年有部电影就叫《Tracker》。
_TORRENT_JUNK_RE = re.compile(
    r"magnet:|urn:btih|\bbtih\b|&dn=|&tr=|&xt=|announce.*\btr\b|\btr\b.*announce",
    re.IGNORECASE,
)


#: 网盘名/页面按钮文案 + 分享 ID。真实形态是
#: `夸克 :https: pan quark cn s 00ab4389973f` —— URL 被拆碎之后只剩下
#: 一个网盘名加一串 hash，`_SHARE_ID_KEY_RE` 因为前面挂着汉字而认不出来。
#: 单行组里这一类占 9.9%（约 5.3 万行）。
_PROVIDER_SHARE_ID_RE = re.compile(
    r"^(?:夸克|百度|阿里|迅雷|天翼|移动|115|123|光鸭|网盘|云盘|链接|磁力"
    r"|查看资源|提取码|访问码|分享链接|分享文件)+[0-9a-f]{6,}$"
)

#: 软件分享。`夸克 6 5 2 338 清爽版 apk` 是网盘客户端的安装包，不是影视作品。
#: 按**子串**认：这几个扩展名不可能出现在片名里。
_SOFTWARE_RE = re.compile(r"apk|exe|msi|dmg|ipa\b")


def looks_like_junk_title(title: str) -> bool:
    """标题是否根本不是一部作品。

    `looks_like_catalog` 判的是"这条文本讲的是一批作品"，这里判的是
    "这条文本压根没讲作品" —— 页面按钮文案、表格字段名、纯数字行号、
    漏进来的分享 ID。两者都该拦在建 media 之前，但原因不同。

    入参是**清洗后**的标题（`clean_title` 的产出）。
    """
    if not title:
        return True

    if _TORRENT_JUNK_RE.search(title):
        # 整条磁力链被当成标题。里面偶尔能看出真片名（`&dn=Shameless US S11`），
        # 但把它抠出来要处理 percent-encoding、tracker 列表、hash，
        # 收益不值得 —— 链接本身是 resource 行，会作为未归属资源留下来。
        return True

    key = norm_key(title)
    if not key:
        # 洗完什么都不剩：原标题全是噪声
        return True
    if len(key) < 2:
        # 单字标题在真实语料里都是切坏的残渣，没有一部作品叫「大」
        return True
    if key.isdigit():
        # `84` `85` `007803` —— 表格行号 / 集数被当成了剧名
        return True
    if key in _JUNK_TITLE_KEYS:
        return True
    if _PROVIDER_SHARE_ID_RE.match(key):
        return True
    if _SOFTWARE_RE.search(key):
        return True
    return bool(_SHARE_ID_KEY_RE.match(key))


# --- 季的识别与剥离 ---------------------------------------------------------

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ROMAN_SEASONS = {"II": 2, "III": 3, "IV": 4}

#: 只认**高置信**的季写法。顺序即优先级。
#:
#: 刻意不认片名末尾的光杆数字（`大主宰2`）—— 它可能是季（大主宰第2季），
#: 也可能是独立续作（《误杀2》是另一部电影）。正则分不出来，交给 LLM 裁决。
#: 同样不认光杆的 `年番`：它指"全年连载"这种播出形式，不等于第一季。
_SEASON_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"第\s*([一二三四五六七八九十]|\d{1,2})\s*[季部]"), "cn"),
    # `年番2` 是季号，`年番 10` 是集数 —— 真实语料里两种都有（`大主宰 年番2`
    # vs `大主宰年番 10`）。靠两个信号区分：中间不能有空格，且只认一位数
    # （没有哪部动画出到第 10 季，但更到第 10 集太常见了）。
    (re.compile(r"年番(\d)(?!\d)"), "digit"),
    # `\bS\d{1,2}\b` 不会匹配 `S01E93` —— `1` 和 `E` 之间没有词边界。
    # 这正是想要的：那是季+集写法，季号由 `_SEASON_EPISODE_RE` 那条路处理。
    (re.compile(r"\bS\s*(\d{1,2})\b", re.IGNORECASE), "digit"),
    # 不能用 `\b` —— Python 的 `\w` 认汉字，`大主宰II` 里「宰」和「I」之间
    # 没有词边界，`\bII\b` 永远匹配不上，而这正是最常见的写法。
    # 改成"左右不是拉丁字母"，这样 `大主宰II` 命中，`VIII`、`DIY` 不会。
    (re.compile(r"(?<![A-Za-z])(I{2,3}|IV)(?![A-Za-z])"), "roman"),
)


def _parse_season_value(raw: str, kind: str) -> int | None:
    if kind == "digit":
        return int(raw)
    if kind == "roman":
        return _ROMAN_SEASONS.get(raw.upper())
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    return _CN_DIGITS.get(raw)


def extract_season(title: str) -> int | None:
    """从标题里认出季号。认不出返回 None（交给 LLM 裁决，不要猜）。

    保守是刻意的：拿真实语料试过激进的写法（把末尾数字、光杆罗马数字都当季号），
    同一组《大主宰》里被抽出 season=2/4/6/8/10 五种值，其中 4 和 10 纯属误报。
    季号错了比缺失更糟 —— 缺失只是进不了季分组，错了会把两季的链接混在一起。
    """
    normalized = unicodedata.normalize("NFKC", title)
    for pattern, kind in _SEASON_RES:
        match = pattern.search(normalized)
        if match is None:
            continue
        season = _parse_season_value(match.group(1), kind)
        if season is not None and 1 <= season <= 99:
            return season
    return None


#: `extract_season` 认得的那些写法，用于把它们从标题里摘掉。
#: 比 `_SEASON_RES` 多认光杆 `年番` —— 它不是季号（所以 extract 不认），
#: 但确实是噪声（所以 strip 要认）。
_SEASON_STRIP_RES = (
    re.compile(r"第\s*(?:[一二三四五六七八九十]|\d{1,2})\s*[季部]"),
    re.compile(r"年番\s*\d{0,2}"),
    re.compile(r"\bS\s*\d{1,2}\b", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z])(?:I{2,3}|IV)(?![A-Za-z])"),
)

#: 片名末尾的光杆续作号：`大主宰2`、`大主宰 2`、`误杀 II`。
#: **只有 `block_key` 用它**，`series_norm_key` 不用 —— 见两者的 docstring。
_TRAILING_SEQUEL_RE = re.compile(r"\s*(?:\d{1,2}|I{1,3}|IV|V)\s*$", re.IGNORECASE)


def strip_season(title: str) -> str:
    """摘掉高置信的季号写法，留下系列名。"""
    text = unicodedata.normalize("NFKC", title)
    for pattern in _SEASON_STRIP_RES:
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -—–_、,，.。|/\\")


#: 外文原名的 token。生产库里同一部片有三种写法：
#: `美国狙击手`、`美国狙击手 American Sniper`、`美国狙击手 American Sniper 年传记片`，
#: 外文名在不在、写不写全各算一个键 —— 这是长尾单行组的第二大来源。
#:
#: 卡「纯拉丁字母且 4 个以上」：《K歌情人》《X战警》《Z风暴》《S风暴》《3D肉蒲团》
#: 里的拉丁字符是**片名本体**，但它们要么只有一两个字母、要么跟汉字同属一个
#: token，都落不进这一条。真实的外文原名、压制组署名、分享 ID 都更长。
_FOREIGN_TOKEN_RE = re.compile(r"^[a-z]{4,}$", re.IGNORECASE)

#: 日文原名的 token（`狼的孩子雨和雪 おおかみこどもの雨と雪`）。
#: 假名混着汉字写，所以按「含假名」判整个 token，不按连续长度判 ——
#: 后者会把 `雨と雪` 里的 `と` 剥掉、留下 `雨雪` 这种碎片。
_KANA_RE = re.compile(r"[ぁ-ゟ゠-ヿ]")

#: 汉字。用来判断剥掉外文之后还剩不剩得下一个片名。
_HAN_RE = re.compile(r"[一-鿿]")


def _drop_foreign_tokens(cleaned: str) -> str:
    """摘掉外文原名/日文原名的 token。见 `_FOREIGN_TOKEN_RE`。

    在 `clean_title` 的产出上做，**不在归一键上做** —— 键里空格已经抹平，
    `美国狙击手americansniper` 分不出哪里是片名哪里是原名，只能靠正则猜；
    而这里 token 边界还在，`美国狙击手` 和 `American` `Sniper` 是分开的。

    只在**剥完还剩至少 2 个汉字**时才生效。这是安全闸：纯外文片名
    （`Dopesick`）会被整条剥空，那种情况下原样返回、宁可不并。
    """
    tokens = cleaned.split()
    kept = [t for t in tokens if not _FOREIGN_TOKEN_RE.match(t) and not _KANA_RE.search(t)]
    rest = " ".join(kept)
    if len(_HAN_RE.findall(rest)) < 2:
        return cleaned
    return rest


#: 末尾光杆数字，用于识别「被重复的标题 + 续作号」里的那个续作号。
_TRAILING_DIGIT_RE = re.compile(r"^(.*?[^\d\s])\s*(\d{1,2})$")


def _collapse_repeated_tokens(cleaned: str) -> str:
    """折叠被重复采集的标题 token。

    来源站点经常把片名在一条文案里渲染好几遍：生产库里
    `大主宰 第二季 大主宰2 大主宰 年番2 更新EP71` 清洗完还剩三个
    `大主宰`，于是键算成 `大主宰2大主宰`，凭空多出一个 Work。

    两步，顺序要紧：

    1. **先**在原始 token 序列上判续作号。`大主宰2` 的光杆 `2` 只在
       `大主宰` 本身**已经重复出现**（≥2 次）时才摘 —— 那是重复采集的
       指纹。合集文案 `速度与激情 速度与激情9` 里 `速度与激情` 只出现
       一次，不动它：9 是独立一部，摘掉就是不可逆的误并。
    2. 再按首次出现去重。

    判定必须用原始序列：先去重的话 `大主宰` 就只剩一次，第 1 步的
    「≥2 次」永远不成立。
    """
    tokens = cleaned.split()
    if len(tokens) < 2:
        return cleaned

    bare = Counter(tokens)
    normalized = []
    for token in tokens:
        match = _TRAILING_DIGIT_RE.match(token)
        if match is not None and bare[match.group(1)] >= 2:
            normalized.append(match.group(1))
        else:
            normalized.append(token)

    return " ".join(dict.fromkeys(normalized))


def series_norm_key(title: str) -> str:
    """**系列身份键**：同一个系列的各季共享它，不同系列必须不同。

    用于确定性地建 Work 行。只摘高置信季号，所以《误杀》和《误杀2》
    会得到两个不同的键 —— 它们确实是两部独立作品，不能并。

    比 `norm_key` 多做两步：摘掉外文原名（见 `_drop_foreign_tokens`）、
    折叠重复采集的标题 token（见 `_collapse_repeated_tokens`）。
    **都不往 `norm_key` 里加** —— 那个键绑在 `uq_media_identity` 上，而且
    外文原名是要留着进 `Work.original_title` / `aliases` 的，不是垃圾。
    """
    cleaned = _drop_foreign_tokens(strip_season(clean_title(title)))
    return norm_key(_collapse_repeated_tokens(cleaned))


def block_key(title: str) -> str:
    """**分块键**：只用来给 LLM 分组，不代表任何身份。

    比 `series_norm_key` 激进 —— 连末尾光杆数字/罗马数字一起摘掉，
    于是 `大主宰`、`大主宰2`、`大主宰 第2季`、`大主宰II` 落进同一块。

    这是有意的召回优先：同一块里的条目**可能**是同一部，由 LLM 在块内裁决
    哪些真该并、哪些是独立续作。分块错了（该在一块的没在一块）LLM 根本没机会
    纠正；分块宽了只是多给模型几个候选，代价仅仅是 token。
    """
    base = strip_season(clean_title(title))
    prev = None
    while prev != base:
        prev = base
        base = _TRAILING_SEQUEL_RE.sub("", base).strip()
    return norm_key(base)


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
    """从文本中识别画质档位，按 4K / 1080P / 720P / 标清的顺序依次匹配，取先命中者。

    Args:
        text: 待识别的原始文本（标题或正文均可）。

    Returns:
        识别到的画质档位；未命中任何关键词时返回 `Quality.UNKNOWN`。
    """
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
    for pattern in (*_EPISODE_PATTERNS, _CN_SEASON_EPISODE_RE):
        m = pattern.search(normalized)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(0).strip())
    return best[1] if best else None


def extract_size_bytes(text: str) -> int | None:
    """提取文件大小，支持 ``440.41MB`` / ``7.49G`` 等常见写法。"""
    match = _SIZE_RE.search(unicodedata.normalize("NFKC", text))
    if match is None:
        return None
    power = {"K": 1, "M": 2, "G": 3, "T": 4}[match.group("unit")[0].upper()]
    return int(float(match.group("value")) * 1024**power)


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
