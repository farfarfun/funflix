"""全局枚举。

所有枚举都以字符串值落库（`native_enum=False` + `create_constraint=False`），
新增成员不需要写迁移 —— provider 这类会持续扩张的枚举尤其依赖这一点。
"""

from __future__ import annotations

import sqlalchemy as sa

from funflix.compat import StrEnum


def enum_col(py_enum: type[StrEnum], length: int = 32) -> sa.Enum:
    """把 Python StrEnum 映射成 VARCHAR，不生成数据库侧的 CHECK 约束。"""
    return sa.Enum(
        py_enum,
        native_enum=False,
        create_constraint=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class SourceType(StrEnum):
    """采集源类型，决定 `services/collect/registry.py` 挑哪个采集器。

    `UNKNOWN` 是兜底值：源还没判定出类型时先入库，不阻塞采集队列。
    """

    TELEGRAM = "telegram"
    TENCENT_DOCS = "tencent_docs"  # 腾讯文档 - 智能表格
    TENCENT_DOC = "tencent_doc"  # 腾讯文档 - 文本文档
    KDOCS = "kdocs"  # 金山文档 - 多维表格
    WEIBO = "weibo"
    FORUM = "forum"
    WEB = "web"
    RSS = "rss"
    MANUAL = "manual"
    API = "api"
    UNKNOWN = "unknown"


class ParseStatus(StrEnum):
    """原始文本的抽取状态，worker 靠它领取待解析的文本。

    `RUNNING` 是领取后写入的占位值（配合租约防重复领取）；`SKIPPED` 表示
    判定为无需抽取（例如正文里没有任何网盘链接），与 `FAILED` 区分开，
    后者才会进重试。
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class MediaType(StrEnum):
    """作品类型。

    `BOOK` 及之后的几个是**非影视**类型。采集源里混着大量小说、漫画、
    课程、软件的分享（`大主宰 我荒古圣体当为天帝 作者:墨之所想 txt` 是小说，
    不是那部动漫），删掉可惜 —— 它们是真资源，只是不该出现在影视搜索结果里。
    所以保留但**默认不进搜索**，见 `services/search.py` 的 `VIDEO_MEDIA_TYPES`。
    """

    MOVIE = "movie"
    TV = "tv"
    ANIME = "anime"
    VARIETY = "variety"
    DOCUMENTARY = "documentary"
    BOOK = "book"
    COMIC = "comic"
    OTHER = "other"
    UNKNOWN = "unknown"


class Quality(StrEnum):
    """资源清晰度。由 `services/text/normalize.py::extract_quality` 从标题文本归一而来。"""

    UHD_4K = "4k"
    FHD_1080P = "1080p"
    HD_720P = "720p"
    SD = "sd"
    UNKNOWN = "unknown"


class Provider(StrEnum):
    """网盘服务商。

    `CHECKABLE_PROVIDERS` 之外的一律入库但不校验（check_status=unsupported）。
    """

    QUARK = "quark"
    UC = "uc"
    ALIPAN = "alipan"
    BAIDU = "baidu"
    PAN115 = "pan115"
    PAN123 = "pan123"
    MOBILE139 = "mobile139"
    GUANGYA = "guangya"
    CTFILE = "ctfile"
    LANZOU = "lanzou"
    TIANYI = "tianyi"
    XUNLEI = "xunlei"
    MAGNET = "magnet"
    ED2K = "ed2k"
    OTHER = "other"


#: 当前实现了匿名探针、会真正发起校验的网盘。其余 provider 直接置 unsupported。
CHECKABLE_PROVIDERS: frozenset[Provider] = frozenset(
    {Provider.QUARK, Provider.ALIPAN, Provider.UC, Provider.PAN123, Provider.CTFILE}
)


class CheckStatus(StrEnum):
    """网盘链接的校验结论。

    `CHECKING` 是 worker 领取后写入的占位值（配合租约防重复领取）；
    `UNSUPPORTED` 表示该 provider 不在 `CHECKABLE_PROVIDERS` 里、根本不会去探；
    `RATE_LIMITED` 与 `ERROR` 都表示"这次没探出结论"，要重试，不能当成失效。
    """

    UNCHECKED = "unchecked"
    CHECKING = "checking"
    VALID = "valid"
    INVALID = "invalid"
    NEED_PASSWORD = "need_password"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED = "unsupported"
    ERROR = "error"
