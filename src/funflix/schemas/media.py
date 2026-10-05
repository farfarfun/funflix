"""作品与网盘资源的查询出参。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from funflix.base.enums import CheckStatus, MediaType, Provider, Quality
from funflix.models.media import UNKNOWN_YEAR
from funflix.models.tag import TagKind


class TagOut(BaseModel):
    """标签出参。只给维度、展示名和 ID，归一键 `norm_key` 是内部实现不外露。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: TagKind
    name: str


class ResourceOut(BaseModel):
    """一条网盘链接。

    `passcode` 照常返回 —— 没有提取码的链接对使用者没有意义，
    这是一个公开分享聚合站，不是凭据存储。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: Provider
    url: str
    passcode: str | None
    title_raw: str | None
    quality: Quality
    episode_info: str | None
    size_bytes: int | None
    sharer_id: str | None
    sharer_name: str | None
    sharer_avatar_url: str | None
    check_status: CheckStatus
    last_checked_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int = Field(description="被多少条分享文本提到过，可当热度用")


class ProviderVerifyReportOut(BaseModel):
    """按网盘分组的一轮校验战报。

    `claimed` 是本轮领到手的条数，`succeeded + failed` 是探出结论的部分；
    `reclaimed` 来自上一轮租约过期被重捞回来的任务，`abandoned` 是重试
    超限、直接置终态不再重试的条数——后两个持续大于 0 说明有 worker 在反复崩溃。
    """

    provider: Provider
    claimed: int
    succeeded: int
    failed: int
    reclaimed: int
    abandoned: int


class _YearBlanked(BaseModel):
    """共享 `year` 哨兵处理。`Work` 和 `Media` 都用 0 表示年份未知。"""

    model_config = ConfigDict(from_attributes=True)

    #: 0 表示年份未知，出参里统一转成 null
    year: int | None

    @field_validator("year", mode="after")
    @classmethod
    def _blank_unknown_year(cls, value: int | None) -> int | None:
        """库里用 0 当「年份未知」的哨兵（见 models.media.UNKNOWN_YEAR），
        对外统一暴露成 null —— 前端不该知道这个哨兵。"""
        return None if value == UNKNOWN_YEAR else value


class MediaSummary(_YearBlanked):
    """一季的列表项。资源计数走 media 表上的冗余字段，不做聚合查询。"""

    id: uuid.UUID
    title: str
    original_title: str | None
    media_type: MediaType
    poster_url: str | None
    resource_count: int
    valid_resource_count: int


class MediaDetail(MediaSummary):
    """季级详情：带别名、简介、外部 ID 与资源。"""

    norm_key: str
    aliases: list[str]
    overview: str | None
    tmdb_id: int | None
    douban_id: str | None
    imdb_id: str | None
    created_at: datetime
    updated_at: datetime
    tags: list[TagOut] = Field(default_factory=list)
    resources: list[ResourceOut] = Field(default_factory=list)


class SeasonSummary(MediaSummary):
    """作品详情里的一季。

    `season` 是 0 时表示「无季概念」（电影、单季剧、综艺），
    见 `models/media.py` 的 `NO_SEASON` —— 展示层该把它渲染成「正片」而不是「第0季」。
    """

    season: int


class SeasonDetail(SeasonSummary):
    """带资源的一季。资源**可能是截断的**，真实总数看 `resource_count`。"""

    resources: list[ResourceOut] = Field(default_factory=list)


class WorkSummary(_YearBlanked):
    """作品列表项 —— 搜索结果的一行。

    这是搜索的主体：一部剧一条，季数和资源数都是跨季汇总后的反规范化计数
    （`services/counters.refresh_work_counters`），列表页不做聚合查询。
    """

    id: uuid.UUID
    title: str
    original_title: str | None
    media_type: MediaType
    poster_url: str | None
    season_count: int
    resource_count: int
    valid_resource_count: int


class WorkDetail(WorkSummary):
    """作品详情：带别名、简介、外部 ID 与季列表（季下挂资源）。"""

    norm_key: str
    aliases: list[str]
    overview: str | None
    tmdb_id: int | None
    douban_id: str | None
    imdb_id: str | None
    created_at: datetime
    updated_at: datetime
    #: 各季标签去重后的并集。标签在库里挂在季上（`media_tag`），但「国漫」「悬疑」
    #: 这种题材/地区标签描述的是整部剧，所以在作品这一层汇总展示。
    #: `Work` 上没有对应的关系属性，由接口层查出来再塞进来（`v1/works.py`）。
    tags: list[TagOut] = Field(default_factory=list)
    seasons: list[SeasonDetail] = Field(default_factory=list)
