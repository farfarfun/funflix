"""数据库引擎与会话。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from funflix.base.config import Settings, get_settings


def _tune_sqlite(dbapi_conn: Any, _record: Any) -> None:
    """SQLite 的连接级 PRAGMA。

    - foreign_keys：SQLite 默认**不**强制外键，不开的话 ondelete 全是摆设。
    - journal_mode=WAL：允许读写并发，否则 worker 写库时 API 的读会被阻塞。
    - busy_timeout：写锁竞争时等待而非立刻抛 "database is locked"。
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """按配置创建一个新的异步 SQLAlchemy 引擎。

    SQLite 与 PostgreSQL 分别调优：SQLite 走连接级 PRAGMA（见 `_tune_sqlite`），
    内存库额外切到 `StaticPool` 以便跨会话共享；PostgreSQL 开连接池并下发
    `pg_trgm.similarity_threshold`。

    参数：
        settings：显式传入的配置；为 `None` 时回退到 `get_settings()` 的全局配置。

    返回：
        尚未关联到进程级缓存的全新 `AsyncEngine` 实例。多数调用方应优先用
        `get_engine()` 复用单例，只有需要独立引擎（如测试隔离）时才直接调用本函数。
    """
    settings = settings or get_settings()
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}
    if settings.is_sqlite:
        # SQLite 不需要连接池调优，但内存库必须用 StaticPool 才能跨会话共享（测试用）
        if ":memory:" in settings.database_url:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        kwargs["pool_pre_ping"] = True
        if settings.database_url.startswith("postgresql"):
            # 相似度阈值随连接下发，而不是每次查询前先 SET 一条。
            # `%` 操作符从这个 GUC 取阈值 —— 不设的话回落到 PG 默认的 0.3，
            # 只是匹配更严，不会出错。
            kwargs["connect_args"] = {
                "server_settings": {
                    "pg_trgm.similarity_threshold": str(settings.search_trgm_threshold)
                }
            }

    engine = create_async_engine(settings.database_url, **kwargs)
    if settings.is_sqlite:
        event.listen(engine.sync_engine, "connect", _tune_sqlite)
    return engine


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """获取进程级共享的异步引擎单例（懒加载，首次调用时创建）。

    返回：
        全局唯一的 `AsyncEngine`，同一进程内的所有调用返回同一实例。
    """
    return create_engine()


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """获取进程级共享的异步 session 工厂单例（懒加载，首次调用时创建）。

    返回：
        绑定到 `get_engine()` 的 `async_sessionmaker`；`expire_on_commit=False`
        以便提交后仍可读取对象属性，`autoflush=False` 避免隐式 flush。
    """
    return async_sessionmaker(
        get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,  # 提交后仍可读对象属性，避免响应序列化时触发懒加载
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话，异常时回滚。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """脱离请求上下文时用（CLI、后台任务）。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """释放进程级共享引擎持有的连接池，并清空单例缓存。

    仅在 `get_engine()` 已经被调用过（即单例已创建）时才真正释放，避免
    为了关闭一个从未打开过的引擎而意外创建它。应用退出、测试用例收尾时调用。
    """
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
