"""网盘资源查询接口。

与 `/media` 的区别是视角：这里按链接本身筛（哪个网盘、校验成不成功），
用于运维排查「夸克最近是不是大面积失效了」这类问题。

**这个模块整体要 `X-API-Key`**：它按 `provider` / `check_status` 成页吐出
整库的链接与提取码，是运维视角而非产品视角。面向使用者的接口是
`/media` 与 `/media/{id}`，保持开放。

> ⚠️ 这道锁防的是**成批导出的便利**，不是保密。链接本身来自公开频道，
> 而且 `/media/{id}` 无凭据就会返回同一份 url 与 passcode ——
> `/resources` 这层锁挡不住已经拿到某个具体 media id 的人。
> 把这里当成机密边界会得到虚假的安全感。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import func, select

from funflix.api.deps import AdminDep, PageDep, SessionDep, get_or_404
from funflix.base.enums import CheckStatus, Provider
from funflix.models import Resource
from funflix.schemas.common import Page
from funflix.schemas.media import ResourceOut

router = APIRouter(prefix="/resources", tags=["resources"])


@router.get("", response_model=Page[ResourceOut])
async def list_resources(
    session: SessionDep,
    paging: PageDep,
    _: AdminDep,
    provider: Provider | None = None,
    check_status: CheckStatus | None = None,
) -> Page[ResourceOut]:
    """按网盘 / 校验状态翻页。

    排序用 `id` 而不是 `last_seen_at`：后者会被 ingest 在每次同一条链接被
    重新分享时改写，翻页途中行会在页与页之间来回移动 —— 客户端翻到第 2 页
    时会重复看到第 1 页的行，被挤下去的那条则永远看不到。`id` 不可变，
    且与「入库先后」同序，翻页结果稳定。
    """
    conditions = []
    if provider is not None:
        conditions.append(Resource.provider == provider)
    if check_status is not None:
        conditions.append(Resource.check_status == check_status)

    total = await session.scalar(select(func.count()).select_from(Resource).where(*conditions))
    rows = await session.scalars(
        select(Resource)
        .where(*conditions)
        .order_by(Resource.id.desc())
        .offset(paging.offset)
        .limit(paging.size)
    )
    return Page[ResourceOut](
        items=[ResourceOut.model_validate(r) for r in rows],
        total=total or 0,
        page=paging.page,
        size=paging.size,
    )


@router.get("/{resource_id}", response_model=ResourceOut)
async def get_resource(resource_id: uuid.UUID, session: SessionDep, _: AdminDep) -> ResourceOut:
    """单条资源详情。与列表接口同样要 key。

    这里曾经是开放的，理由写的是「知道 id 才查得到」—— 那个推理在 id 还是
    自增整数时是错的：`seq 1 100000` 就能把列表接口上的锁绕过去（issue #2）。
    """
    return ResourceOut.model_validate(
        await get_or_404(session, Resource, resource_id, "资源不存在")
    )
