"""作品上的冗余计数维护。

`media.resource_count` / `media.valid_resource_count` 是给列表页用的冗余字段
（DESIGN §3.3）—— 列表页不该为每一行再跑一次聚合查询。

这里**重算**而不是增减。增减看着更省，但它要求每一条改变关联或校验状态的
路径都记得配一次反向操作，漏一处就永久性地对不上，而且没有任何东西会报错。
重算是幂等的：跑一次就把该作品的两个计数拉回与关联表一致，
补数据也只是把所有 id 传进来再跑一遍。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import CheckStatus
from funflix.models import Media, Resource, Tag, media_resource, media_tag


async def refresh_media_counters(session: AsyncSession, media_ids: Iterable[uuid.UUID]) -> int:
    """按关联表重算资源计数，并物理删除没有资源的作品。"""
    ids = {i for i in media_ids if i is not None}
    if not ids:
        return 0

    rows = (
        await session.execute(
            select(
                media_resource.c.media_id,
                func.count(),
                # 用 SUM(CASE ...) 而不是 COUNT(*) FILTER —— 后者在旧版 SQLite 上没有。
                func.sum(case((Resource.check_status == CheckStatus.VALID, 1), else_=0)),
            )
            .select_from(media_resource)
            .join(Resource, Resource.id == media_resource.c.resource_id)
            .where(media_resource.c.media_id.in_(ids))
            .group_by(media_resource.c.media_id)
        )
    ).all()

    counted = {mid: (total, int(valid or 0)) for mid, total, valid in rows}
    orphan_ids = ids - counted.keys()
    if orphan_ids:
        affected_tag_ids = set(
            await session.scalars(
                select(media_tag.c.tag_id).where(media_tag.c.media_id.in_(orphan_ids))
            )
        )
        await session.execute(delete(media_tag).where(media_tag.c.media_id.in_(orphan_ids)))
        await session.execute(delete(Media).where(Media.id.in_(orphan_ids)))
        if affected_tag_ids:
            actual_count = (
                select(func.count())
                .select_from(media_tag)
                .where(media_tag.c.tag_id == Tag.id)
                .scalar_subquery()
            )
            await session.execute(
                update(Tag).where(Tag.id.in_(affected_tag_ids)).values(media_count=actual_count)
            )

    # 单条 CASE 表达式一次性把整批更新写完，而不是每个作品各发一次 UPDATE——
    # 远程数据库上一次往返 ~100ms，作品多的批次逐条更新代价很高。
    if counted:
        await session.execute(
            update(Media)
            .where(Media.id.in_(counted))
            .values(
                resource_count=case(
                    {mid: total for mid, (total, _valid) in counted.items()}, value=Media.id
                ),
                valid_resource_count=case(
                    {mid: valid for mid, (_total, valid) in counted.items()}, value=Media.id
                ),
            )
        )
    return len(ids)


async def refresh_for_resource(session: AsyncSession, resource_id: uuid.UUID) -> int:
    """重算与某条资源相关联的全部作品的计数。

    校验结果变化后用 —— 一条链接可能属于多部作品（合集），
    只更新其中一部会让其余几部的计数悄悄错掉。
    """
    media_ids = list(
        await session.scalars(
            select(media_resource.c.media_id).where(media_resource.c.resource_id == resource_id)
        )
    )
    return await refresh_media_counters(session, media_ids)
