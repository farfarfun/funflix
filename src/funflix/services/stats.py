"""流水线统计聚合。

CLI 的 `funflix status` 与 API 的 `GET /api/v1/stats` 共用这里的一次聚合，
两边不各写一套 —— 否则加一个维度要改两处，迟早对不上。

分组结果的键统一收敛成字符串（枚举取 `.value`），而不是保留枚举成员：
JSON 序列化需要字符串键，CLI 打印也只用得到字面值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.models import (
    Extraction,
    LinkCheck,
    Media,
    RawDocument,
    Resource,
    Source,
    media_resource,
)


@dataclass(slots=True)
class PipelineStats:
    """流水线各环节的记录数。字段顺序即数据流顺序。"""

    sources_total: int = 0
    sources_enabled: int = 0
    sources_failing: int = 0

    raw_total: int = 0
    raw_by_status: dict[str, int] = field(default_factory=dict)

    extraction_total: int = 0
    extraction_by_model: dict[str, int] = field(default_factory=dict)

    media_total: int = 0
    media_by_type: dict[str, int] = field(default_factory=dict)

    resource_total: int = 0
    resource_by_check: dict[str, int] = field(default_factory=dict)
    resource_by_provider: dict[str, int] = field(default_factory=dict)
    #: 在关联表里没有任何作品指向它的资源数
    resource_orphan: int = 0
    media_resource_total: int = 0

    check_total: int = 0


def _label(key: Any) -> str:
    """枚举成员取字面值，其余原样转字符串。"""
    return str(getattr(key, "value", key))


async def collect_stats(session: AsyncSession) -> PipelineStats:
    """把整条流水线的计数聚合成一个对象。"""

    async def count(model: Any, *conditions: Any) -> int:
        stmt = select(func.count()).select_from(model)
        if conditions:
            stmt = stmt.where(*conditions)
        return await session.scalar(stmt) or 0

    async def group(model: Any, column: Any) -> dict[str, int]:
        rows = await session.execute(
            select(column, func.count()).select_from(model).group_by(column)
        )
        return {_label(key): value for key, value in rows.all()}

    return PipelineStats(
        sources_total=await count(Source),
        sources_enabled=await count(Source, Source.enabled),
        sources_failing=await count(Source, Source.consecutive_failures > 0),
        raw_total=await count(RawDocument),
        raw_by_status=await group(RawDocument, RawDocument.parse_status),
        extraction_total=await count(Extraction),
        extraction_by_model=await group(Extraction, Extraction.model),
        media_total=await count(Media),
        media_by_type=await group(Media, Media.media_type),
        resource_total=await count(Resource),
        resource_by_check=await group(Resource, Resource.check_status),
        resource_by_provider=await group(Resource, Resource.provider),
        resource_orphan=await count(
            Resource,
            ~select(media_resource.c.resource_id)
            .where(media_resource.c.resource_id == Resource.id)
            .exists(),
        ),
        media_resource_total=await count(media_resource),
        check_total=await count(LinkCheck),
    )
