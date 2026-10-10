"""PostgreSQL 搜索后端。

默认跳过；设了 `FUNFLIX_TEST_PG_URL` 才跑：

    FUNFLIX_TEST_PG_URL=postgresql+asyncpg://user@/db?host=/tmp/pg pytest tests/test_search_pg.py

为什么值得单独搭一套：`PgTrgmSearchBackend` 是**生产环境真正会跑的那个后端**，
而其余测试全在 SQLite 上，走的是 `LikeSearchBackend`。两个后端一行代码都不共用
关键词子句，所以 SQLite 全绿完全不能说明 PG 上是对的。

最要紧的是 `test_keyword_query_uses_the_trgm_index`：它盯的不是结果对不对，
而是**查询计划**。`similarity(a, b) > 阈值` 与 `a % b` 结果完全一致，
只有后者能走 GIN 索引 —— 前者退化成全表扫描，结果照样正确，测试照样全绿，
只是慢几百倍。这种退化只有查执行计划才拦得住。

搜索的主体是 `Work`（一部剧），所以这里的索引、种子数据、执行计划断言
全部盯在 `work` 表上。资源筛选（`valid_only` / `provider`）要多穿一层
`work → media → media_resource → resource`，单独一组用例盯那条链路。
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from funflix.base.config import Settings
from funflix.base.enums import CheckStatus, MediaType, Provider
from funflix.models import Base, Media, Resource, Work, media_resource
from funflix.models.base import utcnow
from funflix.services.search import PgTrgmSearchBackend, SearchQuery, get_backend

PG_URL = os.environ.get("FUNFLIX_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="未设置 FUNFLIX_TEST_PG_URL")


@pytest_asyncio.fixture
async def pg_session():
    settings = Settings(database_url=PG_URL)
    engine = create_async_engine(
        settings.database_url,
        connect_args={
            "server_settings": {"pg_trgm.similarity_threshold": str(settings.search_trgm_threshold)}
        },
    )
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # 与 migrations/versions/a1b2c3d4e5f6_pg_trgm_search.py 及
        # b1c2d3e4f5a6（Work 表）保持一致
        for table in ("media", "work"):
            for column in ("norm_key", "title"):
                await conn.execute(
                    text(
                        f"CREATE INDEX ix_{table}_{column}_trgm "
                        f"ON {table} USING gin ({column} gin_trgm_ops)"
                    )
                )

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed(session: AsyncSession, bulk: int = 0) -> list[Work]:
    """三部作品：两部能被「误杀」命中，一部不能。"""
    works = [
        Work(title=t, norm_key=n, media_type=MediaType.MOVIE, year=2024, aliases=[])
        for t, n in [("误杀2", "误杀2"), ("流浪地球", "流浪地球"), ("误杀瞒天记", "误杀瞒天记")]
    ]
    session.add_all(works)
    await session.commit()

    if bulk:
        # 走原生 INSERT ... generate_series，逐条 ORM 插 5 万行要几十秒。
        # `id` 这里由 PG 自己生成 —— 模型上的 uuid7 默认值是 Python 侧的，
        # 绕过 ORM 就不生效，不给值会撞 NOT NULL。
        await session.execute(
            text(
                "INSERT INTO work "
                "(id, title, norm_key, media_type, year, aliases, "
                " season_count, resource_count, valid_resource_count, created_at, updated_at) "
                "SELECT gen_random_uuid(), '填充剧集' || g, '填充剧集' || g, 'tv', 2020, '[]', "
                "       0, 0, 0, now(), now() "
                f"FROM generate_series(1, {bulk}) g"
            )
        )
        await session.commit()
        await _vacuum(session)
    return works


async def _add_season(
    session: AsyncSession,
    work: Work,
    *,
    season: int = 0,
    check_status: CheckStatus = CheckStatus.VALID,
    provider: Provider = Provider.QUARK,
) -> Media:
    """给作品挂一季 + 一条资源。资源筛选要穿四张表，必须有真数据才测得到。"""
    media = Media(
        title=f"{work.title} 第{season}季",
        norm_key=f"{work.norm_key}-{season}",
        media_type=work.media_type,
        year=work.year,
        aliases=[],
        work_id=work.id,
        season=season,
        resource_count=1,
        valid_resource_count=1 if check_status is CheckStatus.VALID else 0,
    )
    share_id = uuid.uuid4().hex[:12]
    now = utcnow()
    resource = Resource(
        provider=provider,
        share_id=share_id,
        url=f"https://example.com/s/{share_id}",
        check_status=check_status,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add_all([media, resource])
    await session.flush()
    await session.execute(
        insert(media_resource).values(media_id=media.id, resource_id=resource.id, created_at=now)
    )
    await session.commit()
    return media


async def _vacuum(session: AsyncSession) -> None:
    """VACUUM ANALYZE，把 GIN 的 pending list 合并进索引主体。

    GIN 默认开着 fastupdate：新插入的行先进一个待合并列表，不直接写索引。
    列表没合并前规划器会认为这个索引很贵（实测 5 万行批量导入后，位图扫描
    启动代价 2515，于是它选了顺序扫描），合并之后降到 64，才会真正走索引。

    生产上 autovacuum 会做这件事，所以这不是缺陷；但**大批量导入之后
    到自动清理跑起来之前，关键词搜索会明显偏慢**，赶时间就手动 VACUUM 一次。
    """
    engine = session.bind
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM ANALYZE work"))


@pytest.mark.asyncio
class TestDialectDispatch:
    async def test_postgres_picks_trgm_backend(self, pg_session) -> None:
        """SQLite 上「选对了」和「选错了」都会落到 LikeBackend，分辨不出来。"""
        assert isinstance(get_backend(pg_session), PgTrgmSearchBackend)


@pytest.mark.asyncio
class TestTrgmSearch:
    async def test_finds_by_substring(self, pg_session) -> None:
        works = await _seed(pg_session)
        # 必须真的挂上资源：空壳作品默认不进结果（见 `SearchQuery.include_empty`），
        # 不挂的话这条用例会在「关键词没匹配上」和「被空壳过滤挡掉」之间分辨不出来。
        await _add_season(pg_session, works[0], season=1)
        await _add_season(pg_session, works[2], season=1)
        rows = await PgTrgmSearchBackend().search(pg_session, SearchQuery(keyword="误杀"))
        assert {w.title for w in rows} == {"误杀2", "误杀瞒天记"}

    async def test_count_agrees_with_search(self, pg_session) -> None:
        """count 与 search 是两条独立语句，过滤条件必须一致。

        不一致的话 total 和实际能翻到的行数对不上，前端翻页器会指向空页。

        三部都挂上资源，否则空壳过滤会把结果清成 0 —— 两边都是 0 照样"一致"，
        这条用例就验不到任何东西了。
        """
        works = await _seed(pg_session)
        for work in works:
            await _add_season(pg_session, work, season=1)
        backend = PgTrgmSearchBackend()
        for keyword in ["误杀", "流浪", "不存在的剧", ""]:
            query = SearchQuery(keyword=keyword, limit=100)
            rows = await backend.search(pg_session, query)
            total = await backend.count(pg_session, query)
            assert total == len(rows), f"关键词 {keyword!r}：total={total} 实际={len(rows)}"

    async def test_wildcards_are_literal(self, pg_session) -> None:
        await _seed(pg_session)
        backend = PgTrgmSearchBackend()
        assert await backend.count(pg_session, SearchQuery(keyword="%")) == 0
        assert await backend.count(pg_session, SearchQuery(keyword="_")) == 0

    async def test_filters_compose_with_keyword(self, pg_session) -> None:
        await _seed(pg_session)
        rows = await PgTrgmSearchBackend().search(
            pg_session, SearchQuery(keyword="误杀", media_type=MediaType.TV)
        )
        assert rows == []

    async def test_one_row_per_work_not_per_season(self, pg_session) -> None:
        """这是整次改造的目标：一部剧一条，不管它有几季。

        回归的是旧行为 —— 搜索打在 `media` 上时，《大主宰》的 448 条标题变体
        会原样铺在结果里。现在季是子层，搜索结果里一部剧只出现一次。
        """
        works = await _seed(pg_session)
        for season in (1, 2, 3):
            await _add_season(pg_session, works[0], season=season)
        # 《误杀瞒天记》也要有资源，否则它作为空壳被默认过滤掉，这条用例就只剩
        # 一行结果，"一部剧只占一行"也就无从验证了。
        await _add_season(pg_session, works[2], season=1)

        backend = PgTrgmSearchBackend()
        query = SearchQuery(keyword="误杀", limit=100)
        rows = await backend.search(pg_session, query)

        # 《误杀2》有 3 季，但它在结果里只占一行。打在 media 上的话是 3 行。
        assert sorted(w.title for w in rows) == ["误杀2", "误杀瞒天记"]
        assert await backend.count(pg_session, query) == 2

    async def test_seasons_are_loaded_on_demand(self, pg_session) -> None:
        """`with_seasons` 打开时季要随查询一起回来。

        异步会话下懒加载抛 `MissingGreenlet`，所以不能让调用方拿到对象
        之后再访问 `.seasons` —— 必须在构语句时就预加载。
        """
        works = await _seed(pg_session)
        await _add_season(pg_session, works[0], season=1)
        await _add_season(pg_session, works[0], season=2)
        pg_session.expunge_all()

        rows = await PgTrgmSearchBackend().search(
            pg_session, SearchQuery(keyword="误杀2", with_seasons=True)
        )
        assert [s.season for s in rows[0].seasons] == [1, 2]


@pytest.mark.asyncio
class TestResourceFiltersCrossSeasons:
    """`valid_only` / `provider` 要穿 `work → media → media_resource → resource`。

    季级搜索时这两个子查询只走三张表，多穿一层是改造里最容易写错的地方：
    漏掉 `Media.work_id == Work.id` 的关联条件，EXISTS 会退化成「库里存在
    任意一条可用资源」—— 永远为真，筛选静默失效，而结果看着还挺正常。
    """

    async def test_valid_only_needs_a_valid_resource_on_some_season(self, pg_session) -> None:
        works = await _seed(pg_session)
        await _add_season(pg_session, works[0], season=1, check_status=CheckStatus.INVALID)
        await _add_season(pg_session, works[2], season=1, check_status=CheckStatus.VALID)

        backend = PgTrgmSearchBackend()
        query = SearchQuery(keyword="误杀", valid_only=True, limit=100)
        rows = await backend.search(pg_session, query)

        assert [w.title for w in rows] == ["误杀瞒天记"]
        assert await backend.count(pg_session, query) == 1

    async def test_provider_filter_crosses_the_season_hop(self, pg_session) -> None:
        works = await _seed(pg_session)
        await _add_season(pg_session, works[0], season=1, provider=Provider.QUARK)
        await _add_season(pg_session, works[2], season=1, provider=Provider.ALIPAN)

        backend = PgTrgmSearchBackend()
        query = SearchQuery(keyword="误杀", provider=Provider.ALIPAN, limit=100)
        rows = await backend.search(pg_session, query)

        assert [w.title for w in rows] == ["误杀瞒天记"]
        assert await backend.count(pg_session, query) == 1

    async def test_work_without_seasons_is_filtered_out(self, pg_session) -> None:
        """没有季（也就没有资源）的作品不该通过资源筛选。

        `canon` 流水线会留下一批空壳 Work（季被并走或被判垃圾删掉，
        作品本身刻意保留做审计，见 `canon/apply.py`）。它们不该出现在
        带资源筛选的搜索结果里。
        """
        await _seed(pg_session)
        backend = PgTrgmSearchBackend()
        assert await backend.count(pg_session, SearchQuery(keyword="误杀", valid_only=True)) == 0


@pytest.mark.asyncio
class TestEmptyWorksAreHiddenByDefault:
    """空壳作品（一条资源都没有）**默认**就不进结果，不需要带任何筛选参数。

    这是线上报出来的问题：界面上一大片「0 条资源」，点进去什么都没有。
    `db prune-works` 会删它们，但空壳是流水线持续产出的中间态（资源被清理 /
    季被搬走 / 作品被合并），两次清理之间照样会攒出一批 —— 所以搜索层必须自己拦。

    和 `valid_only` 是两件事：那个要求「有一条**校验通过**的资源」，这个只要求
    「有资源」。一条还没校验的链接对使用者仍然可能有用，不该被藏起来。
    """

    async def test_bare_work_is_invisible(self, pg_session) -> None:
        await _seed(pg_session)
        backend = PgTrgmSearchBackend()
        query = SearchQuery(keyword="误杀", limit=100)
        assert await backend.search(pg_session, query) == []
        assert await backend.count(pg_session, query) == 0, "count 必须和 search 用同一套过滤"

    async def test_unchecked_resource_is_enough(self, pg_session) -> None:
        """只要有资源就可见，不要求校验通过 —— 否则等于偷偷打开了 valid_only。"""
        works = await _seed(pg_session)
        await _add_season(pg_session, works[0], season=1, check_status=CheckStatus.UNCHECKED)

        backend = PgTrgmSearchBackend()
        query = SearchQuery(keyword="误杀", limit=100)
        assert [w.title for w in await backend.search(pg_session, query)] == ["误杀2"]
        assert await backend.count(pg_session, query) == 1

    async def test_include_empty_brings_them_back(self, pg_session) -> None:
        """运维要看清理前的全量时还能拿到。"""
        await _seed(pg_session)
        backend = PgTrgmSearchBackend()
        query = SearchQuery(keyword="误杀", include_empty=True, limit=100)
        assert {w.title for w in await backend.search(pg_session, query)} == {"误杀2", "误杀瞒天记"}
        assert await backend.count(pg_session, query) == 2

    async def test_a_season_without_resources_does_not_count(self, pg_session) -> None:
        """挂着季但季上没有资源，照样是空壳。

        这正是 `db prune-works` 以前收不干净的那一类：`NOT EXISTS media` 判它
        不空，于是它留在库里继续显示成「0 条资源」。
        """
        works = await _seed(pg_session)
        pg_session.add(
            Media(
                title="误杀2 第1季",
                norm_key="误杀2-1",
                media_type=works[0].media_type,
                year=works[0].year,
                aliases=[],
                work_id=works[0].id,
                season=1,
            )
        )
        await pg_session.commit()

        backend = PgTrgmSearchBackend()
        assert await backend.count(pg_session, SearchQuery(keyword="误杀2", limit=100)) == 0


@pytest.mark.asyncio
class TestNonVideoIsHiddenByDefault:
    """小说/漫画默认不进搜索结果，显式传 `media_type` 才看得到。"""

    async def test_book_is_excluded_unless_asked_for(self, pg_session) -> None:
        anime = Work(
            title="大主宰",
            norm_key="大主宰",
            media_type=MediaType.ANIME,
            year=2023,
            aliases=[],
        )
        book = Work(
            title="大主宰（小说）",
            norm_key="大主宰小说",
            media_type=MediaType.BOOK,
            year=0,
            aliases=[],
        )
        pg_session.add_all([anime, book])
        await pg_session.commit()
        # 两部都挂资源：这条用例验的是类型可见性，不该被空壳过滤顺带藏掉。
        await _add_season(pg_session, anime, season=1)
        await _add_season(pg_session, book, season=1)

        backend = PgTrgmSearchBackend()
        default = await backend.search(pg_session, SearchQuery(keyword="大主宰", limit=100))
        assert [w.title for w in default] == ["大主宰"]

        books = await backend.search(
            pg_session, SearchQuery(keyword="大主宰", media_type=MediaType.BOOK, limit=100)
        )
        assert [w.title for w in books] == ["大主宰（小说）"]

    async def test_unknown_type_stays_visible(self, pg_session) -> None:
        """`unknown` 是「还没判出类型」，不是「不是影视」。

        生产库里 40 多万部作品是这个值，把它当非影视排掉等于把它们整体藏起来。
        """
        work = Work(
            title="某部没判出类型的剧",
            norm_key="某部没判出类型的剧",
            media_type=MediaType.UNKNOWN,
            year=0,
            aliases=[],
        )
        pg_session.add(work)
        await pg_session.commit()
        # 挂上资源，否则它会因为「空壳」而不可见，验不到「类型未知仍可见」。
        await _add_season(pg_session, work, season=1)

        rows = await PgTrgmSearchBackend().search(
            pg_session, SearchQuery(keyword="没判出类型", limit=100)
        )
        assert [w.title for w in rows] == ["某部没判出类型的剧"]


@pytest.mark.asyncio
class TestIndexIsActuallyUsed:
    async def test_keyword_query_uses_the_trgm_index(self, pg_session) -> None:
        """回归闸门：关键词查询必须走索引，不能退化成全表扫描。

        `similarity(a,b) > 阈值` 换回来的话结果依然正确、其余测试依然全绿，
        只是从位图索引扫描退化成顺序扫描（实测 5 万行 0.235ms → 63.9ms）。
        只有查执行计划才拦得住这种退化。

        数据量要够大规划器才会选索引 —— 几千行的表顺序扫描本来就更划算，
        那种情况下出现 Seq Scan 是对的，不代表写法有问题。
        """
        await _seed(pg_session, bulk=50_000)

        backend = PgTrgmSearchBackend()
        query = SearchQuery(keyword="误杀2")
        key, _similarity = backend._similarity(query)

        from sqlalchemy import select

        stmt = select(Work.id).where(backend._keyword_clause(query, key))
        compiled = stmt.compile(
            dialect=pg_session.bind.dialect, compile_kwargs={"literal_binds": True}
        )
        plan = "\n".join(
            r[0] for r in (await pg_session.execute(text(f"EXPLAIN {compiled}"))).all()
        )

        assert "Bitmap Index Scan" in plan, f"关键词查询退化成了全表扫描：\n{plan}"
        assert "Seq Scan" not in plan, f"计划里出现了 Seq Scan：\n{plan}"
