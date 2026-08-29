"""原始文本相关的 API 出入参。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from funflix.base.enums import ParseStatus, SourceType

# Page 已挪到 schemas.common（media / resource 等模块同样要用）。
# 这里保留再导出，避免 `from funflix.schemas.raw import Page` 的旧引用失效。
from funflix.schemas.common import Page

__all__ = [
    "BatchIngestResult",
    "IngestResult",
    "Page",
    "RawDocumentBatchCreate",
    "RawDocumentCreate",
    "RawDocumentOut",
    "RawDocumentSummary",
]


class RawDocumentCreate(BaseModel):
    """提交一条原始分享文本。"""

    content: str = Field(min_length=1, description="原始文本全文，原样提交，不要预处理")
    source_id: uuid.UUID | None = Field(default=None, description="采集源 ID；手工提交可不填")
    source_type: SourceType = SourceType.UNKNOWN
    source_name: str | None = Field(default=None, max_length=128, description="频道名 / 站点名")
    source_url: str | None = Field(default=None, max_length=1024, description="原帖链接")
    source_msg_id: str | None = Field(default=None, max_length=128, description="来源侧消息 ID")
    published_at: datetime | None = Field(default=None, description="原帖发布时间，需带时区")
    extra: dict[str, Any] = Field(default_factory=dict, description="来源侧任意元信息")


class RawDocumentBatchCreate(BaseModel):
    items: list[RawDocumentCreate] = Field(min_length=1)


class IngestResult(BaseModel):
    """摄入结果。`duplicated=True` 表示命中 content_hash，返回的是已存在的记录。"""

    id: uuid.UUID
    content_hash: str
    duplicated: bool
    parse_status: ParseStatus


class BatchIngestResult(BaseModel):
    total: int
    created: int
    duplicated: int
    items: list[IngestResult]


class RawDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content: str
    content_hash: str
    source_type: SourceType
    source_name: str | None
    source_url: str | None
    source_msg_id: str | None
    published_at: datetime | None
    collected_at: datetime
    extra: dict[str, Any]
    parse_status: ParseStatus
    parse_attempts: int
    parse_error: str | None
    created_at: datetime
    updated_at: datetime


class RawDocumentSummary(BaseModel):
    """列表项：不含 content 全文，避免列表接口返回巨量文本。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_hash: str
    source_type: SourceType
    source_name: str | None
    collected_at: datetime
    parse_status: ParseStatus
    parse_attempts: int
