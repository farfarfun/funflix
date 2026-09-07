"""采集源相关的 API 出入参。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from funflix.base.enums import SourceType


class SourceCreate(BaseModel):
    """登记一个采集源。

    只传 `url` 即可，类型与标识会自动识别（如 https://t.me/s/Xxx → telegram / Xxx）。
    """

    url: str = Field(min_length=1, max_length=1024, description="采集源地址")
    source_type: SourceType | None = Field(default=None, description="留空则按 url 自动识别")
    identifier: str | None = Field(default=None, max_length=128, description="留空则自动提取")
    title: str | None = Field(default=None, max_length=255)
    enabled: bool = True
    fetch_interval_seconds: int = Field(default=900, ge=30, le=86400)
    max_pages_per_fetch: int = Field(default=5, ge=1, le=50)
    #: 指定后首次采集从该消息 ID 之后开始；留空则只取最新一页并就地立水位
    cursor_message_id: str | None = Field(default=None, max_length=64)


class SourceUpdate(BaseModel):
    title: str | None = None
    enabled: bool | None = None
    fetch_interval_seconds: int | None = Field(default=None, ge=30, le=86400)
    max_pages_per_fetch: int | None = Field(default=None, ge=1, le=50)
    #: 手工回拨水位即可重采历史
    cursor_message_id: str | None = None


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: SourceType
    url: str
    identifier: str
    title: str | None
    enabled: bool
    fetch_interval_seconds: int
    max_pages_per_fetch: int
    cursor_message_id: str | None
    cursor_published_at: datetime | None
    last_fetched_at: datetime | None
    last_success_at: datetime | None
    next_fetch_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    total_collected: int
    # 不是 ORM 列，只在 API 层拼装（见 api/v1/sources.py::_with_stats）——
    # 默认值 0 只是让 from_attributes 校验时（ORM 对象上没这几个属性）不报
    # missing，实际值总是由 _with_stats 通过 model_copy 覆盖。
    raw_total: int = Field(default=0, description="该源产出的原始文本数")
    raw_parsed: int = Field(default=0, description="其中已完成解析（parse_status=done）的数量")
    resource_total: int = Field(default=0, description="从这些原始文本里解析出的网盘资源数")
    created_at: datetime
    updated_at: datetime


class CollectReportOut(BaseModel):
    """一次采集的结果。"""

    source_id: uuid.UUID
    ok: bool
    fetched: int = Field(description="本轮拉到的水位之后的消息数")
    created: int = Field(description="新落库的原始文本数")
    duplicated: int = Field(description="命中 content_hash 被去重的数量")
    skipped_empty: int = Field(description="无正文（纯图片/视频）而跳过的数量")
    pages_fetched: int
    truncated: bool = Field(description="True 表示撞到翻页上限，还有更早的新消息没取完")
    cursor_before: str | None
    cursor_after: str | None
    error: str | None = None


class SourceParseReportOut(BaseModel):
    """一次（单批、不排空队列）解析触发的结果。"""

    source_id: uuid.UUID
    claimed: int = Field(description="本次领取到的原始文本数")
    succeeded: int = Field(description="成功解析（含判定为目录帖 skipped）的数量")
    failed: int = Field(description="解析失败的数量")
    reclaimed: int = Field(description="从过期租约里重捞回来的条数")
    abandoned: int = Field(description="重捞时已超重试上限、直接置 failed 的条数")
    remaining_pending: int = Field(description="该源仍处于待解析状态的原始文本数")
