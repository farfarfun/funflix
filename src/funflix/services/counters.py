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
from funflix.models import Media, Resource, Tag, Work, media_resource, media_tag


async def lock_tags_in_order(
    session: AsyncSession, tag_ids: Iterable[uuid.UUID]
) -> list[uuid.UUID]:
    """按 id 升序把这批 `tag` 行先锁上，返回排好序的 id。

    `tag` 是全库最热的几张行：只有一千多个标签，而几乎每条文档都挂着
    「夸克」「电视剧」。CI 里同时有三路在写它们的 `media_count` ——
    parse 八个分片的增量 UPDATE（`extract/runner._apply_tag_count_deltas`）、
    canon 删垃圾行后的重算（`canon/purge._recount_tags`）、以及这里的
    孤儿清理。加锁顺序对不上就是死锁，实测过一次三方环：

        Process 3551 waits for ShareLock on transaction 5162233; blocked by 3574.
        Process 3574 ... blocked by 3608.
        Process 3608 ... blocked by 3551.

    那次牺牲的是 canon 的 Merge 步，整个 job 退出 1（run 37841925687）。

    **光把 `IN` 列表排序是不够的** —— 那只是个集合字面量，Postgres 按执行
    计划的扫描顺序访问行、不按列表顺序，于是一边走索引扫（id 序）、另一边
    走位图堆扫（物理序）时顺序照样对不上。`ORDER BY id ... FOR UPDATE` 才
    有保证：`LockRows` 节点挂在 `Sort` 之上，锁是按排序后的输出顺序取的。

    代价是每批多一条走主键索引的语句，几毫秒。SQLite 的方言会把
    `FOR UPDATE` 渲染成空串，本地测试走到这里相当于只做了排序。

    Returns:
        去掉 `None`、去重并升序排好的 tag id；空集合返回空列表（不发语句）。
    """
    ids = sorted({i for i in tag_ids if i is not None})
    if ids:
        await session.execute(
            select(Tag.id).where(Tag.id.in_(ids)).order_by(Tag.id).with_for_update()
        )
    return ids


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
            # 先按 id 升序锁上再改，理由见 `lock_tags_in_order`。
            ordered = await lock_tags_in_order(session, affected_tag_ids)
            await session.execute(
                update(Tag).where(Tag.id.in_(ordered)).values(media_count=actual_count)
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


async def refresh_work_counters(session: AsyncSession, work_ids: Iterable[uuid.UUID]) -> int:
    """按下属各季重算作品的季数与资源计数。

    **从 `media` 的冗余列上汇总，不重新 join 到 `resource`** —— 季级计数由
    `refresh_media_counters` 维护，这里再往上滚一层。两级都是重算，所以
    「先刷季、再刷作品」跑完就一定自洽；反过来先刷作品的话作品数会停在
    旧的季级计数上，不会报错但会悄悄对不上，所以调用方必须按这个顺序。

    与 `refresh_media_counters` 不同，这里**不删空作品**：一个作品的季全删完
    通常意味着归并过程中间态（季被并到别的作品上去了），而不是"这部剧没了"。
    真正的孤儿作品由 `canon` 流程收尾时统一清理。
    """
    ids = {i for i in work_ids if i is not None}
    if not ids:
        return 0

    rows = (
        await session.execute(
            select(
                Media.work_id,
                func.count(),
                func.coalesce(func.sum(Media.resource_count), 0),
                func.coalesce(func.sum(Media.valid_resource_count), 0),
            )
            .where(Media.work_id.in_(ids))
            .group_by(Media.work_id)
        )
    ).all()
    counted = {wid: (seasons, int(total), int(valid)) for wid, seasons, total, valid in rows}

    # 一条 CASE 把整批写完，理由同 refresh_media_counters：远端库往返很贵。
    # 没有任何季的作品要显式归零 —— 它不会出现在 GROUP BY 结果里，
    # 不补这一笔的话计数会停在旧值上。
    empty = ids - counted.keys()
    if empty:
        await session.execute(
            update(Work)
            .where(Work.id.in_(empty))
            .values(season_count=0, resource_count=0, valid_resource_count=0)
        )
    if counted:
        await session.execute(
            update(Work)
            .where(Work.id.in_(counted))
            .values(
                season_count=case(
                    {wid: seasons for wid, (seasons, _t, _v) in counted.items()}, value=Work.id
                ),
                resource_count=case(
                    {wid: total for wid, (_s, total, _v) in counted.items()}, value=Work.id
                ),
                valid_resource_count=case(
                    {wid: valid for wid, (_s, _t, valid) in counted.items()}, value=Work.id
                ),
            )
        )
    return len(ids)


async def refresh_counters_for_media(session: AsyncSession, media_ids: Iterable[uuid.UUID]) -> int:
    """刷一批季的计数，顺带把它们所属的作品也刷上 —— **两级联动的默认入口**。

    除了归并流水线（`canon/*` 自己按组分别刷两级，顺序见那里的说明），
    其余所有改动过关联或校验状态的路径都该用这个，而不是直接调
    `refresh_media_counters`：搜索列表展示的是 `Work` 上的计数，季级刷完
    不往上滚一层的话，作品会一直停在旧值 —— 入库了新资源却显示不出来，
    而且不会报任何错。

    作品归属必须在刷季**之前**问出来：`refresh_media_counters` 的契约是顺手
    物理删除零资源的季，行删掉之后就再也查不到它曾属于哪部作品了
    （`canon/purge.py` 的删除路径栽在同一件事上）。

    Returns:
        实际处理的季数，与 `refresh_media_counters` 一致。
    """
    ids = {i for i in media_ids if i is not None}
    if not ids:
        return 0
    work_ids = set(await session.scalars(select(Media.work_id).where(Media.id.in_(ids))))
    touched = await refresh_media_counters(session, ids)
    await refresh_work_counters(session, work_ids)
    return touched


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
    return await refresh_counters_for_media(session, media_ids)
