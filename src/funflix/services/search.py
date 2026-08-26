"""作品搜索。

按数据库方言选实现：PostgreSQL 用 `pg_trgm` 做模糊匹配并按相似度排序，
其余方言回落到 `LIKE`。两者返回同样的结构，调用方无感知。

为什么必须换掉 `LIKE %x%`：前缀通配让索引完全用不上，每次查询全表扫描。
几百条时无所谓，到几万条就是秒级响应变几十秒。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import CheckStatus, MediaType
from funflix.models import Media, Resource, media_resource
from funflix.services.text.normalize import norm_key

logger = logging.getLogger(__name__)

#: pg_trgm 相似度阈值。低于它的结果基本是噪声。
_TRGM_THRESHOLD = 0.2


@dataclass(slots=True)
class SearchQuery:
    keyword: str = ""
    media_type: MediaType | None = None
    year: int | None = None
    #: 只返回至少有一条可用资源的作品
    valid_only: bool = False
    limit: int = 20
    offset: int = 0


@runtime_checkable
class SearchBackend(Protocol):
    name: str

    async def search(self, session: AsyncSession, query: SearchQuery) -> list[Media]: ...

    async def count(self, session: AsyncSession, query: SearchQuery) -> int: ...


def _apply_filters(stmt: Select, query: SearchQuery) -> Select:
    """非关键词的筛选条件，两个后端共用。"""
    if query.media_type is not None:
        stmt = stmt.where(Media.media_type == query.media_type)
    if query.year is not None:
        stmt = stmt.where(Media.year == query.year)
    if query.valid_only:
        # 至少有一条校验通过的资源。用 EXISTS 而不是 JOIN —— 后者会因为
        # 一部作品有多条资源而产生重复行，还得再 DISTINCT。
        stmt = stmt.where(
            select(media_resource.c.media_id)
            .join(Resource, Resource.id == media_resource.c.resource_id)
            .where(
                media_resource.c.media_id == Media.id,
                Resource.check_status == CheckStatus.VALID,
            )
            .exists()
        )
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
        conditions = [Media.title.icontains(query.keyword, autoescape=True)]
        if key:
            conditions.append(Media.norm_key.icontains(key, autoescape=True))
        return or_(*conditions)

    async def search(self, session: AsyncSession, query: SearchQuery) -> list[Media]:
        stmt = select(Media)
        clause = self._keyword_clause(query)
        if clause is not None:
            stmt = stmt.where(clause)
        stmt = _apply_filters(stmt, query)
        stmt = stmt.order_by(Media.id.desc()).offset(query.offset).limit(query.limit)
        return list(await session.scalars(stmt))

    async def count(self, session: AsyncSession, query: SearchQuery) -> int:
        stmt = select(func.count()).select_from(Media)
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
        return key, func.similarity(Media.norm_key, key)

    def _keyword_clause(self, query: SearchQuery, key: str, similarity):
        return or_(
            similarity > _TRGM_THRESHOLD,
            # 子串命中要保底放行：短关键词（「误杀」查「误杀2」）
            # 的 trigram 相似度可能低于阈值，但用户明显想要它。
            Media.norm_key.contains(key, autoescape=True),
            Media.title.icontains(query.keyword, autoescape=True),
        )

    async def search(self, session: AsyncSession, query: SearchQuery) -> list[Media]:
        stmt = select(Media)

        if query.keyword:
            key, similarity = self._similarity(query)
            stmt = stmt.where(self._keyword_clause(query, key, similarity))
            stmt = _apply_filters(stmt, query)
            stmt = stmt.order_by(similarity.desc(), Media.id.desc())
        else:
            stmt = _apply_filters(stmt, query)
            stmt = stmt.order_by(Media.id.desc())

        stmt = stmt.offset(query.offset).limit(query.limit)
        return list(await session.scalars(stmt))

    async def count(self, session: AsyncSession, query: SearchQuery) -> int:
        stmt = select(func.count()).select_from(Media)
        if query.keyword:
            key, similarity = self._similarity(query)
            stmt = stmt.where(self._keyword_clause(query, key, similarity))
        return await session.scalar(_apply_filters(stmt, query)) or 0


def get_backend(session_or_bind: Any) -> SearchBackend:
    """按方言选后端。"""
    bind = getattr(session_or_bind, "bind", session_or_bind)
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect == "postgresql":
        return PgTrgmSearchBackend()
    return LikeSearchBackend()


async def search_media(session: AsyncSession, query: SearchQuery) -> list[Media]:
    backend = get_backend(session)
    logger.debug("搜索后端=%s 关键词=%r", backend.name, query.keyword)
    return await backend.search(session, query)


async def count_media(session: AsyncSession, query: SearchQuery) -> int:
    """与 `search_media` 同条件的总数，供翻页用。

    `limit` / `offset` 在这里无意义，会被忽略。
    """
    return await get_backend(session).count(session, query)
