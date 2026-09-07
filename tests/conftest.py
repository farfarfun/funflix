from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from funflix.api.app import create_app
from funflix.base.db import get_session
from funflix.models import Base, User
from funflix.security import hash_password

#: 测试用的登录账号。运维接口的读写都要先登录。
ADMIN_USERNAME = "tester"
ADMIN_PASSWORD = "test-password"


@pytest_asyncio.fixture
async def engine() -> AsyncIterator:
    """每个测试一个独立的内存库。

    StaticPool 让所有连接复用同一个内存数据库 —— 否则 :memory: 每开一条连接
    就是一个全新的空库，建表和查询会落在不同的库上。
    """
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def client(engine) -> AsyncIterator[AsyncClient]:
    app = create_app()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    # 绕过 lifespan：真实 lifespan 会连全局引擎，测试要用的是上面的内存库
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def admin_client(engine, session) -> AsyncIterator[AsyncClient]:
    """已登录的客户端，用于需要登录态的运维接口。"""
    session.add(User(username=ADMIN_USERNAME, password_hash=hash_password(ADMIN_PASSWORD)))
    await session.commit()

    app = create_app()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        assert resp.status_code == 200
        yield c
