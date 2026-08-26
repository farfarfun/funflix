"""流水线统计接口 —— `funflix status` 的 HTTP 版本。

不要求 API Key：只返回各表的计数，不含链接、文本或凭据。
写接口（`/sources` 的增删改与触发采集）已由 `AdminDep` 保护。

注意它并不便宜：每次调用会跑十几条全表 COUNT，其中还有一次
resource × media_resource 的反连接。目前既无缓存也无限流，
真要对外开放的话得先加一层 TTL 缓存。
"""

from __future__ import annotations

from fastapi import APIRouter

from funflix.api.deps import SessionDep
from funflix.schemas.stats import PipelineStatsOut
from funflix.services.stats import collect_stats

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("", response_model=PipelineStatsOut)
async def get_stats(session: SessionDep) -> PipelineStatsOut:
    """采集 → 抽取 → 作品/资源 → 校验，各环节的记录数与分布。"""
    return PipelineStatsOut.model_validate(await collect_stats(session))
