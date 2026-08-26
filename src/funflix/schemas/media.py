"""作品与网盘资源的查询出参。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from funflix.base.enums import CheckStatus, MediaType, Provider, Quality
from funflix.models.media import UNKNOWN_YEAR
from funflix.models.tag import TagKind


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: TagKind
    name: str


class ResourceOut(BaseModel):
    """一条网盘链接。

    `passcode` 照常返回 —— 没有提取码的链接对使用者没有意义，
    这是一个公开分享聚合站，不是凭据存储。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: Provider
    url: str
    passcode: str | None
    title_raw: str | None
    quality: Quality
    episode_info: str | None
    size_bytes: int | None
    check_status: CheckStatus
    last_checked_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int = Field(description="被多少条分享文本提到过，可当热度用")


class MediaSummary(BaseModel):
    """列表项。资源计数走 media 表上的冗余字段，不做聚合查询。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    original_title: str | None
    media_type: MediaType
    #: 0 表示年份未知，出参里统一转成 null
    year: int | None
    poster_url: str | None
    resource_count: int
    valid_resource_count: int

    @field_validator("year", mode="after")
    @classmethod
    def _blank_unknown_year(cls, value: int | None) -> int | None:
        """库里用 0 当「年份未知」的哨兵（见 models.media.UNKNOWN_YEAR），
        对外统一暴露成 null —— 前端不该知道这个哨兵。"""
        return None if value == UNKNOWN_YEAR else value


class MediaDetail(MediaSummary):
    """详情：带别名、简介、外部 ID 与全部资源。"""

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
