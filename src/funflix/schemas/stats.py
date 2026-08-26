"""流水线统计出参。字段与 `services.stats.PipelineStats` 一一对应。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PipelineStatsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sources_total: int
    sources_enabled: int
    sources_failing: int = Field(description="连续失败次数 > 0 的采集源")

    raw_total: int
    raw_by_status: dict[str, int]

    extraction_total: int
    extraction_by_model: dict[str, int]

    media_total: int
    media_by_type: dict[str, int]

    resource_total: int
    resource_by_check: dict[str, int]
    resource_by_provider: dict[str, int]
    resource_orphan: int = Field(description="没有关联到任何作品的资源")
    media_resource_total: int

    check_total: int
