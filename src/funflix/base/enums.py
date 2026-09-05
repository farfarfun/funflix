"""全局枚举。

所有枚举都以字符串值落库（`native_enum=False` + `create_constraint=False`），
新增成员不需要写迁移 —— provider 这类会持续扩张的枚举尤其依赖这一点。
"""

from __future__ import annotations

from enum import StrEnum

import sqlalchemy as sa


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
    TELEGRAM = "telegram"
    TENCENT_DOCS = "tencent_docs"  # 腾讯文档 - 智能表格
    TENCENT_DOC = "tencent_doc"  # 腾讯文档 - 文本文档
    WEIBO = "weibo"
    FORUM = "forum"
    RSS = "rss"
    MANUAL = "manual"
    API = "api"
    UNKNOWN = "unknown"


class ParseStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class MediaType(StrEnum):
    MOVIE = "movie"
    TV = "tv"
    ANIME = "anime"
    VARIETY = "variety"
    DOCUMENTARY = "documentary"
    UNKNOWN = "unknown"


class Quality(StrEnum):
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
    LANZOU = "lanzou"
    TIANYI = "tianyi"
    XUNLEI = "xunlei"
    MAGNET = "magnet"
    ED2K = "ed2k"
    OTHER = "other"


#: 当前实现了匿名探针、会真正发起校验的网盘。其余 provider 直接置 unsupported。
CHECKABLE_PROVIDERS: frozenset[Provider] = frozenset({Provider.QUARK, Provider.ALIPAN, Provider.UC})


class CheckStatus(StrEnum):
    UNCHECKED = "unchecked"
    CHECKING = "checking"
    VALID = "valid"
    INVALID = "invalid"
    NEED_PASSWORD = "need_password"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED = "unsupported"
    ERROR = "error"
