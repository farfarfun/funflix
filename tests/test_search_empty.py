"""空壳作品默认不进搜索结果。

和 `test_search_pg.py` 的分工：那套盯的是 PG 专有的东西（trgm 子句、执行计划），
默认跳过 —— 没设 `FUNFLIX_TEST_PG_URL` 时它一条都不跑。而「空壳过滤」写在两个
后端共用的 `_apply_filters` 里，所以它必须在**每次都会跑**的 SQLite 套件里也有
闸门，否则改坏了本地全绿、CI 全绿，只有线上能发现。

为什么要这道过滤：界面上一大片「0 条资源」，点进去什么都没有。`db prune-works`
会删它们，但空壳是流水线**持续产出**的中间态（资源被清理 / 季被搬走 / 作品被
合并），两次清理之间照样会攒出一批 —— 删除是收尾，过滤才是常态保障。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import CheckStatus, MediaType, Provider
from funflix.models import Media, Resource, Work, media_resource
from funflix.models.base import utcnow
from funflix.services.search import SearchQuery, count_works, search_works


async def _work(session: AsyncSession, title: str) -> Work:
    work = Work(title=title, norm_key=title, media_type=MediaType.MOVIE, year=2024, aliases=[])
    session.add(work)
    await session.commit()
    return work


async def _season(session: AsyncSession, work: Work, *, resources: int = 1) -> Media:
    """给作品挂一季，再挂 `resources` 条资源（0 条 = 空季）。"""
    media = Media(
        title=f"{work.title} 第1季",
        norm_key=f"{work.norm_key}-1",
        media_type=work.media_type,
        year=work.year,
        aliases=[],
        work_id=work.id,
        season=1,
    )
    session.add(media)
    await session.flush()
    for _ in range(resources):
        share_id = uuid.uuid4().hex[:12]
        now = utcnow()
        resource = Resource(
            provider=Provider.QUARK,
            share_id=share_id,
            url=f"https://pan.quark.cn/s/{share_id}",
            check_status=CheckStatus.UNCHECKED,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(resource)
        await session.flush()
        await session.execute(
            insert(media_resource).values(
                media_id=media.id, resource_id=resource.id, created_at=now
            )
        )
    await session.commit()
    return media


@pytest.mark.asyncio
class TestEmptyWorksHiddenByDefault:
    async def test_work_without_any_season_is_hidden(self, session) -> None:
        await _work(session, "空壳作品")
        query = SearchQuery(keyword="空壳", limit=100)
        assert await search_works(session, query) == []
        assert await count_works(session, query) == 0, "count 必须和 search 用同一套过滤"

    async def test_work_with_an_empty_season_is_hidden(self, session) -> None:
        """挂着季但季上没有资源，照样是空壳。

        这正是 `db prune-works` 以前收不干净的那一类：`NOT EXISTS media` 判它
        不空，于是它留在库里继续在界面上显示成「0 条资源」。
        """
        work = await _work(session, "只有空季的作品")
        await _season(session, work, resources=0)
        assert await count_works(session, SearchQuery(keyword="只有空季", limit=100)) == 0

    async def test_unchecked_resource_is_enough(self, session) -> None:
        """只要有资源就可见，不要求校验通过 —— 否则等于偷偷打开了 `valid_only`。

        一条还没校验的链接对使用者仍然可能有用，不该跟空壳一起藏掉。
        """
        work = await _work(session, "有未校验资源的作品")
        await _season(session, work, resources=1)
        query = SearchQuery(keyword="未校验", limit=100)
        assert [w.title for w in await search_works(session, query)] == ["有未校验资源的作品"]
        assert await count_works(session, query) == 1

    async def test_include_empty_brings_them_back(self, session) -> None:
        """运维要看清理前的全量时还能拿到。"""
        await _work(session, "空壳作品")
        query = SearchQuery(keyword="空壳", include_empty=True, limit=100)
        assert [w.title for w in await search_works(session, query)] == ["空壳作品"]
        assert await count_works(session, query) == 1

    async def test_empty_work_does_not_leak_through_browse(self, session) -> None:
        """不带关键词的列表页（界面默认那一屏）也要挡住。

        报上来的问题就是在这一屏看到的，而列表页走的是空关键词的同一条路。
        """
        bare = await _work(session, "空壳作品")
        live = await _work(session, "有资源的作品")
        await _season(session, live, resources=1)

        titles = [w.title for w in await search_works(session, SearchQuery(limit=100))]
        assert titles == ["有资源的作品"]
        assert bare.title not in titles
        assert await count_works(session, SearchQuery(limit=100)) == 1
