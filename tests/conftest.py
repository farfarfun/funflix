from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from funflix.models import Base


@pytest_asyncio.fixture
async def engine() -> AsyncIterator:
    """每个测试一个独立的内存库。

    StaticPool 让所有连接复用同一个内存数据库 —— 否则 :memory: 每开一条连接
    就是一个全新的空库，建表和查询会落在不同的库上。

    另外接上 SQLAlchemy 文档里那套 pysqlite 事务修正。驱动默认会自己偷偷开
    事务，而 `SAVEPOINT` 不算 DML、不会触发它 —— 于是 savepoint 实际是在
    autocommit 下执行的，`RELEASE` 直接把里面的插入**提交**掉，外层
    `session.rollback()` 再也回滚不掉它。生产库是 Postgres，savepoint 回滚
    是真的，不修正的话这里测出来的语义跟生产正好相反（实测：savepoint 内插
    一行、外层回滚，SQLite 剩 1 行、PG 剩 0 行），凡是用
    `session.begin_nested()` 兜并发冲突的代码就都测不准。
    """
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _disable_driver_begin(dbapi_conn, _record) -> None:
        dbapi_conn.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _emit_explicit_begin(conn) -> None:
        conn.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
