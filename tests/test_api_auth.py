"""登录态鉴权。

「运维」区从共享 key 换成账号密码后，这批测试确保：
- 没登录时，运维接口（sources / raw / resources / stats 的读写）一律 401；
- 产品接口（/media、/healthz）保持匿名可访问；
- 登录成功后能访问上面这些接口，退出登录后又不能了；
- 注册入口默认关闭，只有显式打开配置后才能用，且不能注册重名账号。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from funflix.api.app import create_app
from funflix.base.config import Settings, get_settings
from funflix.base.db import get_session
from funflix.base.enums import ParseStatus
from funflix.models import Extraction, RawDocument, Source, User, utcnow
from funflix.security import hash_password

USERNAME = "tester"
PASSWORD = "test-password"


def _client_with(engine, settings: Settings) -> AsyncClient:
    app = create_app()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: settings
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def anon(engine) -> AsyncIterator[AsyncClient]:
    """没登录的客户端。"""
    async with _client_with(engine, Settings()) as c:
        yield c


@pytest_asyncio.fixture
async def authed(engine, session) -> AsyncIterator[AsyncClient]:
    """已登录的客户端：先建号，再走一遍真实的登录接口拿会话 cookie。"""
    session.add(User(username=USERNAME, password_hash=hash_password(PASSWORD)))
    await session.commit()
    async with _client_with(engine, Settings()) as c:
        resp = await c.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert resp.status_code == 200
        yield c


PAYLOAD = {"url": "https://t.me/s/demo_channel"}


@pytest.mark.asyncio
class TestOpsEndpointsRequireLogin:
    async def test_create_source_without_login_is_rejected(self, anon) -> None:
        assert (await anon.post("/api/v1/sources", json=PAYLOAD)).status_code == 401

    async def test_create_source_after_login_succeeds(self, authed) -> None:
        assert (await authed.post("/api/v1/sources", json=PAYLOAD)).status_code == 201

    async def test_list_sources_requires_login(self, anon) -> None:
        assert (await anon.get("/api/v1/sources")).status_code == 401

    async def test_stats_requires_login(self, anon) -> None:
        """`/stats` 曾经完全匿名开放，换成登录态后一并收进来。"""
        assert (await anon.get("/api/v1/stats")).status_code == 401

    async def test_resources_requires_login(self, anon) -> None:
        assert (await anon.get("/api/v1/resources")).status_code == 401

    async def test_raw_requires_login(self, anon) -> None:
        assert (await anon.get("/api/v1/raw")).status_code == 401
        assert (await anon.post("/api/v1/raw", json={"content": "x"})).status_code == 401

    async def test_delete_requires_login(self, authed) -> None:
        """删源会丢掉水位游标，绝不能匿名。"""
        created = await authed.post("/api/v1/sources", json=PAYLOAD)
        source_id = created.json()["id"]
        assert (await authed.delete(f"/api/v1/sources/{source_id}")).status_code == 204

    async def test_reset_source_cursor(self, authed, session) -> None:
        created = await authed.post("/api/v1/sources", json=PAYLOAD)
        source = await session.get(Source, uuid.UUID(created.json()["id"]))
        assert source is not None
        source.cursor_message_id = "123"
        source.backfill_cursor_id = "100"
        source.backfill_done = True
        source.extra = {"revision": 9}
        await session.commit()

        response = await authed.post(f"/api/v1/sources/{source.id}/reset-cursor")
        await session.refresh(source)

        assert response.status_code == 204
        assert source.cursor_message_id is None
        assert source.backfill_cursor_id is None
        assert source.backfill_done is False
        assert source.extra == {}

    async def test_reset_source_parse(self, authed, session) -> None:
        created = await authed.post("/api/v1/sources", json=PAYLOAD)
        source = await session.get(Source, uuid.UUID(created.json()["id"]))
        assert source is not None
        now = utcnow()
        done = RawDocument(
            content="done",
            content_hash="1" * 64,
            source=source,
            source_type=source.source_type,
            collected_at=now,
            parse_status=ParseStatus.DONE,
            parse_attempts=2,
            last_parsed_at=now,
        )
        skipped = RawDocument(
            content="skipped",
            content_hash="2" * 64,
            source=source,
            source_type=source.source_type,
            collected_at=now,
            parse_status=ParseStatus.SKIPPED,
            parse_attempts=1,
            parse_error="old error",
            lease_until=now,
            next_parse_at=now,
            last_parsed_at=now,
        )
        extraction = Extraction(
            raw_document=done,
            model="test",
            prompt_version="v1",
            output={},
            stats={},
        )
        session.add_all([done, skipped, extraction])
        await session.commit()
        extraction_id = extraction.id

        before = await authed.get(f"/api/v1/sources/{source.id}")
        response = await authed.post(f"/api/v1/sources/{source.id}/reset-parse")
        await session.refresh(done)
        await session.refresh(skipped)
        session.expunge(extraction)

        assert before.json()["raw_parsed"] == 2
        assert response.status_code == 200
        assert response.json() == 2
        for document in (done, skipped):
            assert document.parse_status == ParseStatus.PENDING
            assert document.parse_attempts == 0
            assert document.parse_error is None
            assert document.lease_until is None
            assert document.next_parse_at is None
            assert document.last_parsed_at is None
        assert await session.get(Extraction, extraction_id) is None

    async def test_logout_revokes_access(self, authed) -> None:
        assert (await authed.post("/api/v1/auth/logout")).status_code == 204
        assert (await authed.get("/api/v1/sources")).status_code == 401


@pytest.mark.asyncio
class TestLogin:
    async def test_wrong_password_is_rejected(self, engine, session) -> None:
        session.add(User(username=USERNAME, password_hash=hash_password(PASSWORD)))
        await session.commit()
        async with _client_with(engine, Settings()) as c:
            resp = await c.post(
                "/api/v1/auth/login", json={"username": USERNAME, "password": "wrong"}
            )
            assert resp.status_code == 401

    async def test_unknown_username_is_rejected(self, engine) -> None:
        async with _client_with(engine, Settings()) as c:
            resp = await c.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
            assert resp.status_code == 401

    async def test_me_reflects_session(self, authed) -> None:
        resp = await authed.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == USERNAME

    async def test_me_is_null_when_anonymous(self, anon) -> None:
        resp = await anon.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json() is None


@pytest.mark.asyncio
class TestRegistration:
    async def test_disabled_by_default(self, engine) -> None:
        async with _client_with(engine, Settings(registration_enabled=False)) as c:
            resp = await c.post(
                "/api/v1/auth/register", json={"username": "new", "password": "abcdef"}
            )
            assert resp.status_code == 403

    async def test_enabled_via_config(self, engine) -> None:
        async with _client_with(engine, Settings(registration_enabled=True)) as c:
            resp = await c.post(
                "/api/v1/auth/register", json={"username": "new", "password": "abcdef"}
            )
            assert resp.status_code == 201

    async def test_duplicate_username_is_rejected(self, engine) -> None:
        async with _client_with(engine, Settings(registration_enabled=True)) as c:
            await c.post("/api/v1/auth/register", json={"username": "dup", "password": "abcdef"})
            resp = await c.post(
                "/api/v1/auth/register", json={"username": "dup", "password": "abcdef"}
            )
            assert resp.status_code == 409


@pytest.mark.asyncio
class TestReadEndpointsStayOpen:
    """面向使用者的产品接口不要求登录。"""

    @pytest.mark.parametrize("path", ["/api/v1/media", "/healthz"])
    async def test_open_without_login(self, anon, path) -> None:
        assert (await anon.get(path)).status_code == 200
