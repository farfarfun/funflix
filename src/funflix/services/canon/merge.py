"""把多行 media 就地合并成一行 —— 阶段 2 和阶段 4 共用的原子操作。

改造完之后 media 的身份是 `(work_id, season)`，而历史数据里同一部剧的同一季
散落在几十行上（`大主宰 第2季`、`大主宰 年番2`、`大主宰 S02 4K高码`……）。
重算键之后这些行会落到同一个 `(work_id, season)`，必须并成一行。

**不能删了重建** —— 资源关联、标签、校验历史都挂在 media.id 上。做法是挑一个
存活行，把其余行的关联迁过去，再删掉它们。挑谁存活用「资源最多」：那一行的
关联迁移量最小，而且它通常也是标题最完整的那条。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from farlog import getLogger
from sqlalchemy import Table, delete, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.dbinsert import insert_stmt
from funflix.base.enums import MediaType
from funflix.models import Media, media_resource, media_tag
from funflix.models.media import UNKNOWN_YEAR

logger = getLogger("funflix")

#: 合并时从败者补到存活行上的可空字段。只在存活行该字段为空时才补。
_FILLABLE = ("original_title", "poster_url", "overview", "tmdb_id", "douban_id", "imdb_id")

#: `aliases` 最多留多少条。生产库里一部剧有几百种写法，全留下来会让这一列
#: 比整行其余字段加起来还大，而别名的用途（搜索兜底、人工核对）几十条就够。
MAX_ALIASES = 50


@dataclass(slots=True)
class MergeStats:
    merged: int = 0
    links_moved: int = 0
    links_dropped: int = 0
    tags_moved: int = 0


async def pick_survivor(session: AsyncSession, media_ids: list[uuid.UUID]) -> uuid.UUID:
    """挑资源最多的那一行作为存活行。

    一行资源都没有的 media 也要能被挑中（全组都是空的时候），所以用
    `LEFT JOIN` 语义：没出现在计数结果里的按 0 算，再按 id 定序保证
    同样的输入每次挑出同一行（可重跑时不会换人）。
    """
    counts = dict(
        (
            await session.execute(
                select(media_resource.c.media_id, func.count())
                .where(media_resource.c.media_id.in_(media_ids))
                .group_by(media_resource.c.media_id)
            )
        ).all()
    )
    return max(media_ids, key=lambda mid: (counts.get(mid, 0), str(mid)))


async def _move_assocs(
    session: AsyncSession, table: Table, other: str, loser: uuid.UUID, survivor: uuid.UUID
) -> tuple[int, int]:
    """把一个败者在某张关联表上的行迁到存活行，返回 (迁移数, 丢弃的重复数)。

    **一次只迁一个败者** —— 不能把整组 `media_id.in_(losers)` 一把迁完：两个
    败者挂着同一条资源时，两行都会变成同一个 `(survivor, resource)`，直接撞
    关联表主键。逐个迁的话第二个败者的那条在下一轮里会被当成重复丢掉，这才
    是对的。

    ## 为什么是「插入 + 删除」而不是 UPDATE

    原来这里是 `UPDATE ... WHERE resource_id NOT IN (存活行已有的)`，看着没
    问题：子查询把重复的排除掉了。**但那个子查询只看得见自己事务快照里的
    行。** 同时在跑的 parse 正在给存活行挂新资源，它提交在我们的快照之后，
    于是子查询没排除它、UPDATE 把败者那条改成同一个键 —— 撞主键。

    run 37783618690 的 repair job 就是这么挂的：`duplicate key value violates
    unique constraint "pk_media_resource"`，调用链 `repair apply` →
    `assign_identities` → `merge_media_rows` → 这里。而 `_apply_rehomes` 的
    退避重试没救它 —— 那层只认死锁和序列化失败（SQLSTATE 40001/40P01），
    23505 唯一键冲突不在里面，**而且也不该加进去**：23505 不是普遍瞬时的，
    真有重复插入的逻辑 bug 会被静默重试三次然后吞掉。

    所以治根：`INSERT ... SELECT ... ON CONFLICT DO NOTHING` 把「有没有重复」
    的判断挪进同一条语句里，窗口就不存在了；然后无条件删掉败者剩下的行。
    语义和原来完全一样，只是不再有那个窗口。见 `base/dbinsert.py`。
    """
    src = select(
        literal(survivor, type_=table.c.media_id.type).label("media_id"),
        table.c[other],
        table.c.created_at,
    ).where(table.c.media_id == loser)
    moved = await session.execute(
        insert_stmt(session, table)
        .from_select(["media_id", other, "created_at"], src)
        .on_conflict_do_nothing(index_elements=["media_id", other])
    )
    # 插入之后败者名下**所有**行都该没了：搬过去的那些已经在存活行名下有副本，
    # 撞上重复的那些本来就该丢。所以 `dropped` 是删除数减去迁移数。
    deleted = await session.execute(delete(table).where(table.c.media_id == loser))
    moved_count = moved.rowcount or 0
    return moved_count, max((deleted.rowcount or 0) - moved_count, 0)


async def _move_links(
    session: AsyncSession, loser: uuid.UUID, survivor: uuid.UUID
) -> tuple[int, int]:
    """把一个败者的资源关联迁到存活行，返回 (迁移数, 丢弃的重复数)。"""
    return await _move_assocs(session, media_resource, "resource_id", loser, survivor)


async def _move_tags(session: AsyncSession, loser: uuid.UUID, survivor: uuid.UUID) -> int:
    """把一个败者的标签关联迁到存活行。重复的直接丢（标签没有计数要守）。"""
    moved, _ = await _move_assocs(session, media_tag, "tag_id", loser, survivor)
    return moved


def absorb_attributes(survivor: Media, loser: Media) -> None:
    """把败者身上比存活行更好的信息搬过来。

    三条取舍：

    - `year` 取**较小的非零值**。media 现在代表一季，同一季的不同分享里
      年份经常一个写首播年一个写重播/引进年，首播年才是这一季的年份。
    - `media_type` 只在存活行是 `unknown` 时才被覆盖。两边都是具体类型但
      不一致（`anime` vs `tv`）时**不动** —— 这种分歧要靠 LLM 裁决，
      在这里随便挑一个等于把错误固化下来。
    - 败者的标题进 `aliases`，不覆盖 `title`。存活行是资源最多的那条，
      它的标题是群众投票的结果，比单条败者可信。
    """
    if loser.year != UNKNOWN_YEAR and (survivor.year == UNKNOWN_YEAR or loser.year < survivor.year):
        survivor.year = loser.year
    if survivor.media_type is MediaType.UNKNOWN and loser.media_type is not MediaType.UNKNOWN:
        survivor.media_type = loser.media_type

    for field in _FILLABLE:
        if getattr(survivor, field) is None:
            setattr(survivor, field, getattr(loser, field))

    known = set(survivor.aliases or [])
    known.add(survivor.title)
    extra = [a for a in [loser.title, *(loser.aliases or [])] if a and a not in known]
    if extra:
        # 赋新列表而不是 `.append()` —— `aliases` 是 JSON 列，原地改可变对象
        # SQLAlchemy 默认检测不到（没有 MutableList），改动不会被写回去。
        survivor.aliases = [*(survivor.aliases or []), *dict.fromkeys(extra)][:MAX_ALIASES]


async def merge_media_rows(
    session: AsyncSession, media_ids: list[uuid.UUID], *, survivor_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, MergeStats]:
    """把 `media_ids` 并成一行，返回 (存活行 id, 统计)。

    只 flush 不 commit —— 调用方按组提交，这样中断时不会留下半并完的组。

    `media_ids` 是**快照**：算出这一组到真的来并，中间隔着别的步骤甚至别的
    节点（CI 里 canon 和 repair 是两个并行 job，canon merge 删 media 行，
    repair apply 拿的是几十分钟前 scan 算出来的计划）。所以这里对「id 指向的
    行已经没了」是容错的：缺的那几行当成已经并掉，跳过；连存活行都没了就整组
    不并，留给下一轮重新规划 —— 实测不容错的后果是 `KeyError` 冒到 CLI，
    `repair apply` 整步退出 1，这一轮**其余已经并好的组也跟着回滚**。
    """
    stats = MergeStats()
    if len(media_ids) < 2:
        return (survivor_id or media_ids[0]), stats

    survivor_id = survivor_id or await pick_survivor(session, media_ids)
    rows = {m.id: m for m in await session.scalars(select(Media).where(Media.id.in_(media_ids)))}
    if survivor_id not in rows:
        # 存活行自己被删了，没有能并进去的目标。败者留在原身份上，
        # 下一轮 scan 会重新规划 —— 比随便换个存活行安全：这一组的
        # 身份归属是上游按「存活行坐在哪」算出来的，换人等于换结论。
        logger.warning(
            f"合并跳过：存活行 {survivor_id} 已不存在，{len(media_ids) - 1} 个败者留待下轮"
        )
        return survivor_id, stats

    survivor = rows[survivor_id]
    loser_ids = [mid for mid in media_ids if mid != survivor_id and mid in rows]
    if gone := len(media_ids) - 1 - len(loser_ids):
        logger.info(f"合并时有 {gone} 个败者已不存在（别的节点先并掉了），跳过")

    for loser_id in loser_ids:
        moved, dropped = await _move_links(session, loser_id, survivor_id)
        stats.links_moved += moved
        stats.links_dropped += dropped
        stats.tags_moved += await _move_tags(session, loser_id, survivor_id)
        absorb_attributes(survivor, rows[loser_id])
        # 从 session 里摘出去再用 Core 删 —— **不走 `session.delete()`**。
        # ORM 删除要处理 `resources` / `tags` 这两个 secondary 关系，为此会
        # 去加载集合；异步会话里的隐式懒加载直接抛 `MissingGreenlet`。
        # 关联行上一步已经删干净了，这里只剩主表那一行要删。
        session.expunge(rows[loser_id])
        stats.merged += 1

    if loser_ids:
        await session.execute(delete(Media).where(Media.id.in_(loser_ids)))
    await session.flush()
    return survivor_id, stats
