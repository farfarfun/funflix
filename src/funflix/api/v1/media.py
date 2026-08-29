"""作品查询接口（DESIGN §7.2）。

列表走 `services.search` 的后端抽象：PG 上是 pg_trgm 模糊匹配，
其余方言回落 LIKE，调用方无感知。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from funflix.api.deps import PageDep, SessionDep
from funflix.base.enums import CheckStatus, MediaType
from funflix.models import Media, Resource, media_resource
from funflix.models.media import UNKNOWN_YEAR
from funflix.schemas.common import Page
from funflix.schemas.media import MediaDetail, MediaSummary
from funflix.services.search import SearchQuery, count_media, search_media

#: 详情页最多返回多少条资源。`resource_count` 仍是真实总数。
MAX_DETAIL_RESOURCES = 200

router = APIRouter(prefix="/media", tags=["media"])


@router.get("", response_model=Page[MediaSummary])
async def list_media(
    session: SessionDep,
    paging: PageDep,
    keyword: str = Query(
        default="", max_length=128, description="剧名关键词，留空则按入库时间倒序"
    ),
    media_type: MediaType | None = None,
    year: int | None = Query(
        default=None,
        ge=UNKNOWN_YEAR,
        le=2100,
        description=f"年份；传 {UNKNOWN_YEAR} 查年份未知的作品（出参里这些作品的 year 是 null）",
    ),
    valid_only: bool = Query(default=False, description="只要至少有一条校验通过资源的作品"),
) -> Page[MediaSummary]:
    """搜索 / 浏览作品。"""
    query = SearchQuery(
        keyword=keyword.strip(),
        media_type=media_type,
        year=year,
        valid_only=valid_only,
        limit=paging.size,
        offset=paging.offset,
    )
    total = await count_media(session, query)
    rows = await search_media(session, query)
    return Page[MediaSummary](
        items=[MediaSummary.model_validate(r) for r in rows],
        total=total,
        page=paging.page,
        size=paging.size,
    )


@router.get("/{media_id}", response_model=MediaDetail)
async def get_media(media_id: uuid.UUID, session: SessionDep) -> MediaDetail:
    """作品详情，含网盘资源与标签。

    关联对象一律预加载 —— 异步会话下懒加载会在序列化时抛 MissingGreenlet，
    而不是悄悄多发几条查询。

    资源最多返回 `MAX_DETAIL_RESOURCES` 条，且可用的排在前面。热门剧集会被
    很多频道反复分享，`media_resource` 只增不删，全量返回能到几 MB ——
    而使用者要的只是「一条能用的链接」。总数看 `resource_count`。
    """
    media = await session.scalar(
        select(Media).where(Media.id == media_id).options(selectinload(Media.tags))
    )
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作品不存在")

    rows = list(
        await session.scalars(
            select(Resource)
            .join(media_resource, media_resource.c.resource_id == Resource.id)
            .where(media_resource.c.media_id == media_id)
            # 可用的排前面，其余按入库倒序
            .order_by(
                case((Resource.check_status == CheckStatus.VALID, 0), else_=1),
                Resource.id.desc(),
            )
            .limit(MAX_DETAIL_RESOURCES)
        )
    )
    # 用 set_committed_value 而不是直接赋值：直接给关系属性赋值会被 ORM 当成
    # 「这就是全部关联」，flush 时把没列进来的关联行删掉 —— 截断展示会变成截断数据。
    set_committed_value(media, "resources", rows)
    return MediaDetail.model_validate(media)
