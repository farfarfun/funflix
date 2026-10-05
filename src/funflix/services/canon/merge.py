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

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import MediaType
from funflix.models import Media, media_resource, media_tag
from funflix.models.media import UNKNOWN_YEAR

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


async def _move_links(
    session: AsyncSession, loser: uuid.UUID, survivor: uuid.UUID
) -> tuple[int, int]:
    """把一个败者的资源关联迁到存活行，返回 (迁移数, 丢弃的重复数)。

    **一次只迁一个败者** —— 不能把整组 `media_id.in_(losers)` 一条 UPDATE
    迁完：两个败者挂着同一条资源时，两行都会被改成同一个
    `(survivor, resource)`，直接撞关联表主键。逐个迁的话第二个败者的那条
    在下一轮里会被当成重复丢掉，这才是对的。
    """
    dupes = select(media_resource.c.resource_id).where(media_resource.c.media_id == survivor)
    moved = await session.execute(
        update(media_resource)
        .where(media_resource.c.media_id == loser, media_resource.c.resource_id.not_in(dupes))
        .values(media_id=survivor)
    )
    dropped = await session.execute(
        delete(media_resource).where(media_resource.c.media_id == loser)
    )
    return moved.rowcount or 0, dropped.rowcount or 0


async def _move_tags(session: AsyncSession, loser: uuid.UUID, survivor: uuid.UUID) -> int:
    """把一个败者的标签关联迁到存活行。重复的直接丢（标签没有计数要守）。

    同 `_move_links`，逐个败者迁，理由一样。
    """
    dupes = select(media_tag.c.tag_id).where(media_tag.c.media_id == survivor)
    moved = await session.execute(
        update(media_tag)
        .where(media_tag.c.media_id == loser, media_tag.c.tag_id.not_in(dupes))
        .values(media_id=survivor)
    )
    await session.execute(delete(media_tag).where(media_tag.c.media_id == loser))
    return moved.rowcount or 0


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
    """
    stats = MergeStats()
    if len(media_ids) < 2:
        return (survivor_id or media_ids[0]), stats

    survivor_id = survivor_id or await pick_survivor(session, media_ids)
    rows = {m.id: m for m in await session.scalars(select(Media).where(Media.id.in_(media_ids)))}
    survivor = rows[survivor_id]
    loser_ids = [mid for mid in media_ids if mid != survivor_id]

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
