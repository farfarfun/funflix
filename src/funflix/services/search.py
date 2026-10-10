"""作品搜索。

搜索的主体是 **`Work`（一部剧）**，不是 `Media`（一季）—— 用户搜「大主宰」
要的是一条「大主宰（4 季 / 1496 条资源）」，而不是 448 条同名行。季级数据
从 `Work.seasons` 展开，见 `models/work.py`。

按数据库方言选实现：PostgreSQL 用 `pg_trgm` 做模糊匹配并按相似度排序，
其余方言回落到 `LIKE`。两者返回同样的结构，调用方无感知。

为什么必须换掉 `LIKE %x%`：前缀通配让索引完全用不上，每次查询全表扫描。
几百条时无所谓，到几万条就是秒级响应变几十秒。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from farlog import getLogger
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from funflix.base.enums import CheckStatus, MediaType, Provider
from funflix.models import Media, Resource, Work, media_resource
from funflix.services.text.normalize import norm_key

logger = getLogger("funflix")

#: 默认进搜索结果的类型。非影视（`BOOK` / `COMIC` / `OTHER`）要显式传
#: `media_type` 才看得到 —— 小说和漫画是真资源，但搜「大主宰」的人要的是动漫。
#:
#: `UNKNOWN` **在这个集合里**。它是「还没判出类型」而不是「不是影视」，
#: 库里 40 多万部作品是这个值，排掉等于把它们整体藏起来。
#: 新增影视类型时记得加进来，否则它会默认不可见。
VIDEO_MEDIA_TYPES = frozenset(
    {
        MediaType.MOVIE,
        MediaType.TV,
        MediaType.ANIME,
        MediaType.VARIETY,
        MediaType.DOCUMENTARY,
        MediaType.UNKNOWN,
    }
)

#: 相似度阈值不在这里 —— 它由 `pg_trgm.similarity_threshold` 这个 GUC 提供，
#: 随连接参数下发（`Settings.search_trgm_threshold` → base/db.py）。
#: 写进 WHERE 里的话就必须用 `similarity()` 函数形式，那样索引会失效。
#:
#: 另一件必须知道的事：**2 个汉字的关键词在 PG 上也走不了索引**。
#: pg_trgm 要三元组，2 字关键词提不出完整 trigram，`%` 和 `LIKE` 都退化成全表扫描
#: （实测 5 万行 81.9ms）。这是 pg_trgm 的固有限制，不是这里写错了。


@dataclass(slots=True)
class SearchQuery:
    """一次搜索请求的筛选条件与分页参数，由 `LikeSearchBackend` 与
    `PgTrgmSearchBackend` 共用。"""

    keyword: str = ""
    #: 显式指定类型。留空时只返回 `VIDEO_MEDIA_TYPES` 里的类型；
    #: 传 `book` / `comic` / `other` 可以专门查非影视资源。
    media_type: MediaType | None = None
    year: int | None = None
    #: 只返回至少有一条可用资源的作品
    valid_only: bool = False
    #: 连一条资源都没有的作品也返回。默认 **False** —— 这种行对用户是死链，
    #: 点进去什么都没有。打开它只有一个用途：运维要看清理前的全量。
    #:
    #: 为什么必须在搜索层拦而不是只靠 `db prune-works` 定期删：空壳是流水线
    #: **持续产出**的中间态（作品先建行、media 随后挂上，以及资源被清理 /
    #: 季被搬走 / 作品被合并），而 `PRUNE_MIN_AGE` 的静置窗口是故意留的 ——
    #: 删早了会删掉正在入库的行。生产库实测产出速率约每小时 1,000 行，也就是
    #: 任何时刻都有一千行左右坐在闸门里删不掉。过滤是常态保障，删除是收尾。
    include_empty: bool = False
    #: 只返回至少有一条该网盘资源的作品
    provider: Provider | None = None
    limit: int = 20
    offset: int = 0
    #: 顺带预加载季列表。放在这里而不是让调用方自己加 `options()` ——
    #: 构造语句的是后端，调用方插不进去；而异步会话下懒加载会抛
    #: `MissingGreenlet`，不是悄悄多发几条查询。
    #:
    #: 默认关。列表页展示的季数/资源数读 `Work` 上的反规范化计数
    #: （`season_count` / `resource_count`），不需要季行本身。
    with_seasons: bool = False


@runtime_checkable
class SearchBackend(Protocol):
    """搜索后端协议。`get_backend` 按数据库方言在实现间二选一，调用方统一走这层接口。"""

    name: str

    async def search(self, session: AsyncSession, query: SearchQuery) -> list[Work]:
        """按 `query` 的关键词、筛选条件与分页参数查询匹配的作品。

        Args:
            session: 数据库会话，用于执行查询。
            query: 关键词、筛选条件与分页参数（`limit`/`offset`）。

        Returns:
            匹配的作品列表，长度不超过 `query.limit`，具体排序规则由实现决定。
        """
        ...

    async def count(self, session: AsyncSession, query: SearchQuery) -> int:
        """按与 `search` 相同的筛选条件统计匹配总数。

        Args:
            session: 数据库会话，用于执行查询。
            query: 关键词与筛选条件；其中的 `limit`/`offset` 不参与统计。

        Returns:
            匹配的作品总数。
        """
        ...


def _resource_exists(*conditions: Any):
    """「这部作品下存在满足条件的资源」子查询。

    比季级搜索多穿一层：资源挂在 `media`（季）上，而筛选的主体是 `work`，
    所以要 `work → media → media_resource → resource` 走完四张表。

    只问「有没有资源」不要用这个，用 `_has_any_resource` —— 那条路不必碰
    `resource` 表。

    两个函数的子查询形状**故意不一样**，不要去统一：`_has_any_resource` 嵌成
    两层能省一半时间，而这里带条件的情况嵌起来没有收益（生产库 `count` 全量实测，
    `valid_only` 444ms→407ms、`provider` 537ms→548ms，都在噪声里）—— 条件落在
    `resource` 上，那张表无论怎么写都得碰，嵌套省掉的恰恰是不必碰它这件事。
    """
    return (
        select(media_resource.c.media_id)
        .join(Media, Media.id == media_resource.c.media_id)
        .join(Resource, Resource.id == media_resource.c.resource_id)
        .where(Media.work_id == Work.id, *conditions)
        .exists()
    )


def _has_any_resource():
    """「这部作品下至少有一条资源」—— 不带条件时的专用形状，只走三张表。

    **为什么可以不碰 `resource` 表**：`media_resource.resource_id` 是
    `ondelete="CASCADE"` 的外键（见 `models/association.py`），关联行不可能比
    它指向的资源行活得久 —— 存在一条关联行就等于存在一条资源。生产库核对过：
    孤儿关联行 0 行，两种写法数出来一致。

    **为什么要嵌成两层**而不是 `media JOIN media_resource`：两种写法语义相同，
    但规划器对嵌套形式给的计划好得多 —— 它先在 `media` 和 `media_resource`
    之间做半连接，再和 `work` 散列，不用把 184 万行关联表整个摊进一次大散列。

    生产库实测（31 万作品 / 32 万季 / 184 万关联，`count` 全量、缓存预热后中位）：

    | 写法 | 耗时 |
    | --- | --- |
    | 不带空壳过滤（基线） | 110ms |
    | 四表 `_resource_exists()` | 875ms |
    | 三表（去掉 `resource`） | 467ms |
    | 嵌套两层（本函数） | **337ms** |

    `count` 是翻页每次都要的，所以这 2.6 倍直接落在每个列表页上。拿 20 行的
    `search` 两种写法都是亚毫秒（半连接够到 `limit` 就短路），差别只在 `count`。
    """
    return (
        select(Media.id)
        .where(
            Media.work_id == Work.id,
            select(media_resource.c.media_id).where(media_resource.c.media_id == Media.id).exists(),
        )
        .exists()
    )


def _apply_options(stmt: Select, query: SearchQuery) -> Select:
    """实体加载选项，两个后端的 `search` 共用（`count` 不需要）。"""
    if query.with_seasons:
        stmt = stmt.options(selectinload(Work.seasons))
    return stmt


def _apply_filters(stmt: Select, query: SearchQuery) -> Select:
    """非关键词的筛选条件，两个后端共用。"""
    if query.media_type is not None:
        stmt = stmt.where(Work.media_type == query.media_type)
    else:
        stmt = stmt.where(Work.media_type.in_(sorted(VIDEO_MEDIA_TYPES)))
    if query.year is not None:
        stmt = stmt.where(Work.year == query.year)
    # 下面两个筛选都要求「存在一条满足更严条件的资源」，已经蕴含了「至少有一条
    # 资源」。它们在场时再加一道空壳过滤只是让规划器多干一遍活，结果一行不差。
    narrower = query.valid_only or query.provider is not None
    if not query.include_empty and not narrower:
        # 空壳作品（一条资源都没有）不进结果。
        #
        # 判据用真实存在性而不是 `Work.resource_count > 0`：那是 `counters.py`
        # 事后重算的冗余列，漏刷一次就会把**有资源**的作品也藏起来 —— 藏错比
        # 多显示一行严重得多。生产库实测过这不是假想：10,255 行显示「0 条资源」
        # 的作品里有 1,831 行**真的有资源**，只是计数没刷到。
        stmt = stmt.where(_has_any_resource())
    if query.valid_only:
        # 至少有一条校验通过的资源。用 EXISTS 而不是 JOIN —— 后者会因为
        # 一部作品有多条资源而产生重复行，还得再 DISTINCT。
        stmt = stmt.where(_resource_exists(Resource.check_status == CheckStatus.VALID))
    if query.provider is not None:
        # 与 valid_only 是两个独立条件，不要求同一条资源既 valid 又是该网盘 ——
        # 这样两个筛选可以自由组合，语义更符合直觉。
        stmt = stmt.where(_resource_exists(Resource.provider == query.provider))
    return stmt


class LikeSearchBackend:
    """`LIKE` 兜底实现。小数据量够用，大表会全表扫描。"""

    name = "like"

    def _keyword_clause(self, query: SearchQuery):
        """关键词条件；无关键词时返回 None。search 与 count 共用。

        用 `icontains(autoescape=True)` 而不是手拼 `ilike(f"%{kw}%")` ——
        后者会把用户输入里的 `%` 和 `_` 当成通配符：搜 `%` 命中全表，
        搜 `S01_1080p` 里的下划线能匹配任意字符。分享标题里这两个符号很常见。
        """
        if not query.keyword:
            return None
        key = norm_key(query.keyword)
        conditions = [Work.title.icontains(query.keyword, autoescape=True)]
        if key:
            conditions.append(Work.norm_key.icontains(key, autoescape=True))
        return or_(*conditions)

    async def search(self, session: AsyncSession, query: SearchQuery) -> list[Work]:
        """按查询条件返回匹配的作品列表。"""
        stmt = select(Work)
        clause = self._keyword_clause(query)
        if clause is not None:
            stmt = stmt.where(clause)
        stmt = _apply_filters(stmt, query)
        stmt = _apply_options(stmt, query)
        stmt = stmt.order_by(Work.id.desc()).offset(query.offset).limit(query.limit)
        return list(await session.scalars(stmt))

    async def count(self, session: AsyncSession, query: SearchQuery) -> int:
        """按查询条件返回匹配的作品数量。"""
        stmt = select(func.count()).select_from(Work)
        clause = self._keyword_clause(query)
        if clause is not None:
            stmt = stmt.where(clause)
        return await session.scalar(_apply_filters(stmt, query)) or 0


class PgTrgmSearchBackend:
    """PostgreSQL `pg_trgm` 实现：容错匹配 + 按相似度排序。

    相比 `LIKE`，它能命中错字和词序颠倒（「误杀2」↔「误杀 II」），
    并且有 GIN 索引支撑，不会随数据量线性劣化。
    """

    name = "pg_trgm"

    def _similarity(self, query: SearchQuery):
        key = norm_key(query.keyword) or query.keyword
        return key, func.similarity(Work.norm_key, key)

    def _keyword_clause(self, query: SearchQuery, key: str):
        return or_(
            # 用 `%` 操作符而不是 `similarity(a, b) > 阈值`。
            #
            # 两者语义相同，但只有操作符形式能走 GIN gin_trgm_ops 索引 ——
            # 函数调用形式规划器只能全表扫描，而且它 OR 在最前面，会把整个
            # 子句一起拖下水，另外两个分支的索引也用不上了。
            # 实测 5 万行、3 字关键词：63.9ms → 0.235ms。
            #
            # 阈值来自 `pg_trgm.similarity_threshold`，由连接参数下发，见 base/db.py。
            Work.norm_key.bool_op("%")(key),
            # 子串命中要保底放行：短关键词（「误杀」查「误杀2」）
            # 的 trigram 相似度可能低于阈值，但用户明显想要它。
            Work.norm_key.contains(key, autoescape=True),
            Work.title.icontains(query.keyword, autoescape=True),
        )

    async def search(self, session: AsyncSession, query: SearchQuery) -> list[Work]:
        """按查询条件返回匹配的作品列表，有关键词时按相似度降序排列。

        Args:
            session: 数据库会话。
            query: 关键词、筛选条件与分页参数。

        Returns:
            匹配的作品列表；有关键词时按 `pg_trgm` 相似度降序、再按 id 降序；
            无关键词时仅按 id 降序。
        """
        stmt = select(Work)

        if query.keyword:
            key, similarity = self._similarity(query)
            stmt = stmt.where(self._keyword_clause(query, key))
            stmt = _apply_filters(stmt, query)
            stmt = stmt.order_by(similarity.desc(), Work.id.desc())
        else:
            stmt = _apply_filters(stmt, query)
            stmt = stmt.order_by(Work.id.desc())

        stmt = _apply_options(stmt, query)
        stmt = stmt.offset(query.offset).limit(query.limit)
        return list(await session.scalars(stmt))

    async def count(self, session: AsyncSession, query: SearchQuery) -> int:
        """按与 `search` 相同的条件（含关键词的相似度/子串匹配）统计匹配总数。

        Args:
            session: 数据库会话。
            query: 关键词与筛选条件；`limit`/`offset` 不参与统计。

        Returns:
            匹配的作品总数。
        """
        stmt = select(func.count()).select_from(Work)
        if query.keyword:
            key, similarity = self._similarity(query)
            stmt = stmt.where(self._keyword_clause(query, key))
        return await session.scalar(_apply_filters(stmt, query)) or 0


def get_backend(session_or_bind: Any) -> SearchBackend:
    """按方言选后端。"""
    bind = getattr(session_or_bind, "bind", session_or_bind)
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect == "postgresql":
        return PgTrgmSearchBackend()
    return LikeSearchBackend()


async def search_works(session: AsyncSession, query: SearchQuery) -> list[Work]:
    """按数据库方言自动选择后端（PostgreSQL 用 `pg_trgm`，其余用 `LIKE`）并执行搜索。

    Args:
        session: 数据库会话，既用于判断方言，也用于实际执行查询。
        query: 关键词、筛选条件与分页参数。

    Returns:
        匹配的作品列表，具体排序规则由所选后端决定。要季列表就把
        `query.with_seasons` 打开 —— 异步会话下懒加载会抛 `MissingGreenlet`。
    """
    backend = get_backend(session)
    logger.debug(f"搜索后端={backend.name} 关键词={query.keyword!r}")
    return await backend.search(session, query)


async def count_works(session: AsyncSession, query: SearchQuery) -> int:
    """与 `search_works` 同条件的总数，供翻页用。

    `limit` / `offset` 在这里无意义，会被忽略。
    """
    return await get_backend(session).count(session, query)
