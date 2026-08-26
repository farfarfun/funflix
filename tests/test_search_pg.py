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
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from funflix.base.config import Settings
from funflix.base.enums import MediaType
from funflix.models import Base, Media
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
        # 与 migrations/versions/a1b2c3d4e5f6_pg_trgm_search.py 保持一致
        await conn.execute(
            text("CREATE INDEX ix_media_norm_key_trgm ON media USING gin (norm_key gin_trgm_ops)")
        )
        await conn.execute(
            text("CREATE INDEX ix_media_title_trgm ON media USING gin (title gin_trgm_ops)")
        )

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed(session: AsyncSession, bulk: int = 0) -> None:
    session.add_all(
        [
            Media(title=t, norm_key=n, media_type=MediaType.MOVIE, year=2024, aliases=[])
            for t, n in [("误杀2", "误杀2"), ("流浪地球", "流浪地球"), ("误杀瞒天记", "误杀瞒天记")]
        ]
    )
    await session.commit()

    if bulk:
        # 走原生 INSERT ... generate_series，逐条 ORM 插 5 万行要几十秒。
        await session.execute(
            text(
                "INSERT INTO media "
                "(title, norm_key, media_type, year, aliases, "
                " resource_count, valid_resource_count, created_at, updated_at) "
                "SELECT '填充剧集' || g, '填充剧集' || g, 'tv', 2020, '[]', "
                "       0, 0, now(), now() "
                f"FROM generate_series(1, {bulk}) g"
            )
        )
        await session.commit()
        await _vacuum(session)


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
        await conn.execute(text("VACUUM ANALYZE media"))


@pytest.mark.asyncio
class TestDialectDispatch:
    async def test_postgres_picks_trgm_backend(self, pg_session) -> None:
        """SQLite 上「选对了」和「选错了」都会落到 LikeBackend，分辨不出来。"""
        assert isinstance(get_backend(pg_session), PgTrgmSearchBackend)


@pytest.mark.asyncio
class TestTrgmSearch:
    async def test_finds_by_substring(self, pg_session) -> None:
        await _seed(pg_session)
        rows = await PgTrgmSearchBackend().search(pg_session, SearchQuery(keyword="误杀"))
        assert {m.title for m in rows} == {"误杀2", "误杀瞒天记"}

    async def test_count_agrees_with_search(self, pg_session) -> None:
        """count 与 search 是两条独立语句，过滤条件必须一致。

        不一致的话 total 和实际能翻到的行数对不上，前端翻页器会指向空页。
        """
        await _seed(pg_session)
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
        key, similarity = backend._similarity(query)

        from sqlalchemy import select

        stmt = select(Media.id).where(backend._keyword_clause(query, key, similarity))
        compiled = stmt.compile(
            dialect=pg_session.bind.dialect, compile_kwargs={"literal_binds": True}
        )
        plan = "\n".join(
            r[0] for r in (await pg_session.execute(text(f"EXPLAIN {compiled}"))).all()
        )

        assert "Bitmap Index Scan" in plan, f"关键词查询退化成了全表扫描：\n{plan}"
        assert "Seq Scan" not in plan, f"计划里出现了 Seq Scan：\n{plan}"
