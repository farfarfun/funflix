"""把 media 行搬到目标 `(work_id, season)` 上，撞车就地合并。

阶段 2（`rebuild`）和阶段 4（`apply`）都要做这件事，而两边最早的写法都是
「先一条 UPDATE 全搬过去，再回头 GROUP BY 找重复并掉」。那在 `uq_media_season`
存在的库上**跑不过去** —— UPDATE 还没写完第二行就撞唯一键了。

所以搬迁必须自带合并：同一个目标身份上有几行，就先定存活行、再把其余行并进去，
任何时刻库里都不存在重复的 `(work_id, season)`。这样 `rebuild` 在迁移 B 之后
也能再跑（改了归一规则想重新分组，是个正常需求），不再是只能跑一次的一次性工具。

## 换位冲突与两趟搬迁

有一种情形先搬谁都会撞：A 现在在 `(W,1)` 要去 `(W,2)`，而 B 现在在 `(W,2)`
要去 `(W,1)`。两边互相挡路，单趟里**无论先搬谁都撞唯一键**，换个顺序也救不了
（这是个环）。

这里不靠捕异常，也不靠「推迟到下次重跑」—— 后者在真正成环时永远解不开。
做法是**先腾地方**：

1. 第一趟，目标身份上坐着的那行如果自己也要搬走，就把本组成员先挪到一个
   临界的负数 season（`_PARK_BASE` 往下数，真实数据里不会出现负季号）。
   挪完之后，所有「要搬走的行」都已经离开了自己的原身份。
2. 第二趟，那些原身份已经空了，直接落位。

所以最多两趟一定收敛，不存在搬不动的情形；第三趟还有冲突说明有别的东西在
并发改表，那是真的异常，直接抛。

停车用的负 season 只活在一次调用内部（函数只 flush 不 commit），
进程中途挂掉会连整个事务一起回滚，库里不会留下负季号。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.models import Media
from funflix.services.canon.merge import merge_media_rows, pick_survivor

#: 身份 = `(work_id, season)`
Identity = tuple[uuid.UUID, int]

#: 临时停车位的 season 起点，往下递减。取这么小是为了和任何真实季号
#: （以及手工写坏的负数）都拉开距离。
_PARK_BASE = -1_000_000

#: 最多搬几趟。两趟足够（见模块说明），第三趟是留给"真的出事了"的断言。
_MAX_PASSES = 3


@dataclass(slots=True)
class AssignStats:
    #: 物理改变了身份的行数（存活行的搬迁，不含被并掉的败者）。
    moved: int = 0
    #: 落到同一个 `(work_id, season)` 上、因而需要合并的身份数。
    conflicts: int = 0
    merged: int = 0
    links_moved: int = 0
    links_dropped: int = 0
    #: 为腾地方而临时挪动的次数。纯粹是诊断信息 —— 大于 0 说明这一批里
    #: 存在换位环，不代表有问题。
    parked: int = 0
    #: 被动到的 work_id（含搬离前的旧归属）—— 收尾重算计数要用。
    touched_works: set[uuid.UUID] = field(default_factory=set)
    #: 吸收了别人关联、因而资源数变了的存活行。收尾只需要刷这些行的季级计数
    #: —— 不能拿「所有被动过的 Work 下的全部 media」去刷，`refresh_media_counters`
    #: 会顺手删掉零资源的行，那会连带删掉这一轮根本没碰过的空壳。
    merged_into: set[uuid.UUID] = field(default_factory=set)


async def _occupants(
    session: AsyncSession, identities: list[Identity]
) -> dict[Identity, uuid.UUID]:
    """查这些身份上现在坐着谁。一次 tuple IN 查完整批。"""
    if not identities:
        return {}
    rows = (
        await session.execute(
            select(Media.id, Media.work_id, Media.season).where(
                tuple_(Media.work_id, Media.season).in_(identities)
            )
        )
    ).all()
    return {(work_id, season): media_id for media_id, work_id, season in rows}


async def _pass(
    session: AsyncSession,
    targets: dict[uuid.UUID, Identity],
    titles: dict[uuid.UUID, str],
    stats: AssignStats,
    park_from: int,
) -> dict[uuid.UUID, Identity]:
    """搬一趟，返回这一趟没搬成、需要下一趟处理的 media → 目标身份。"""
    by_identity: dict[Identity, list[uuid.UUID]] = {}
    for media_id, identity in targets.items():
        by_identity.setdefault(identity, []).append(media_id)

    occupants = await _occupants(session, list(by_identity))
    blocked: dict[uuid.UUID, Identity] = {}
    park_slot = park_from

    for identity, members in by_identity.items():
        work_id, season = identity
        stats.touched_works.add(work_id)
        sitting = occupants.get(identity)

        if sitting is not None and sitting not in members and sitting in targets:
            # 现住户自己也要搬走，但它还没走 —— 先把本组成员挪到停车位，
            # 等它腾出来（这一趟结束时它一定已经离开，见模块说明）。
            for member in members:
                park_slot -= 1
                await session.execute(
                    update(Media).where(Media.id == member).values(season=park_slot)
                )
                stats.parked += 1
            await session.flush()
            blocked.update({member: identity for member in members})
            continue

        if sitting is not None:
            # 身份上已经坐着本组的一行，就让它当存活行 —— 省掉一次搬迁，
            # 也省掉把它的关联迁给别人。
            survivor = sitting
        else:
            survivor = members[0] if len(members) == 1 else await pick_survivor(session, members)

        values: dict[str, object] = {"work_id": work_id, "season": season}
        if survivor in titles:
            values["title"] = titles[survivor]
        if survivor != sitting:
            await session.execute(update(Media).where(Media.id == survivor).values(**values))
            stats.moved += 1
            await session.flush()
        elif survivor in titles:
            await session.execute(
                update(Media).where(Media.id == survivor).values(title=titles[survivor])
            )

        # `survivor` 可能是个「陌生人」—— 目标身份上坐着一行不在本批搬迁名单
        # 里的 media（它本来就归属正确）。那时本组成员全是败者，整组并进它。
        losers = [mid for mid in members if mid != survivor]
        if losers:
            stats.conflicts += 1
            stats.merged_into.add(survivor)
            # 败者此刻还各自坐在自己的旧身份上（互不相同），所以合并途中不会
            # 撞唯一键；并完它们就被删掉了。败者的标题不用刷 —— 行没了。
            _kept, merge_stats = await merge_media_rows(
                session, [survivor, *losers], survivor_id=survivor
            )
            stats.merged += merge_stats.merged
            stats.links_moved += merge_stats.links_moved
            stats.links_dropped += merge_stats.links_dropped

    await session.flush()
    return blocked


async def assign_identities(
    session: AsyncSession,
    targets: dict[uuid.UUID, Identity],
    *,
    extra_titles: dict[uuid.UUID, str] | None = None,
) -> AssignStats:
    """把 `targets` 里的 media 行搬到各自的目标身份，撞车的并成一行。

    Args:
        targets: media_id → 目标 `(work_id, season)`。
        extra_titles: media_id → 要同时刷新的 `title`。只给需要改标题的行传
            （`rebuild` 会按新规则重洗标题，`apply` 不动标题）。

    只 flush 不 commit —— 调用方按批提交，中断时不会留下半搬完的身份
    （包括临时停车位，见模块说明）。
    """
    stats = AssignStats()
    if not targets:
        return stats

    # 搬离前的旧归属也要记下来：那些 Work 的季数/资源数会变小，不刷就停在旧值。
    # 过滤 NULL —— 迁移 B 之前 `work_id` 还是可空的，第一次 `canon rebuild` 时
    # **每一行**的旧归属都是 NULL（那正是这一步要回填的东西）。放进去的话收尾
    # `sorted(touched_works)` 会拿 None 和 UUID 比大小，直接 TypeError。
    for work_id in await session.scalars(select(Media.work_id).where(Media.id.in_(list(targets)))):
        if work_id is not None:
            stats.touched_works.add(work_id)

    titles = extra_titles or {}
    remaining = targets
    for _ in range(_MAX_PASSES):
        remaining = await _pass(session, remaining, titles, stats, _PARK_BASE - stats.parked)
        if not remaining:
            return stats

    raise RuntimeError(
        f"{len(remaining)} 行 media 搬了 {_MAX_PASSES} 趟还在互相挡路。"
        "两趟就该收敛（见 services/canon/assign.py 的模块说明），"
        "出现这种情况说明有并发写入在同时改 media 的 (work_id, season)。"
    )
