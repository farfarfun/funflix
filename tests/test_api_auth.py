"""写接口的鉴权。

这批断言存在的理由：加锁之前，`POST/PATCH/DELETE /sources` 与
`/sources/{id}/collect` 全部匿名可调，而删掉一个源会连带丢掉它的水位游标 ——
重建后要么从头重采、要么漏掉中间的消息。`require_admin` 当时已经写好，
却没有任何路由在用，而且**没有一条测试碰过这些接口**，所以谁都没发现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from funflix.api.app import create_app
from funflix.base.config import Settings, get_settings
from funflix.base.db import get_session

KEY = "test-admin-key"


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
async def keyed(engine) -> AsyncIterator[AsyncClient]:
    """配置了 admin key 的客户端。"""
    async with _client_with(engine, Settings(admin_api_key=KEY)) as c:
        yield c


@pytest_asyncio.fixture
async def keyless(engine) -> AsyncIterator[AsyncClient]:
    """没配 admin key —— 此时管理接口应当整体关闭。"""
    async with _client_with(engine, Settings(admin_api_key=None)) as c:
        yield c


PAYLOAD = {"url": "https://t.me/s/demo_channel"}


@pytest.mark.asyncio
class TestWriteEndpointsRequireKey:
    async def test_create_without_key_is_rejected(self, keyed) -> None:
        assert (await keyed.post("/api/v1/sources", json=PAYLOAD)).status_code == 401

    async def test_create_with_wrong_key_is_rejected(self, keyed) -> None:
        resp = await keyed.post("/api/v1/sources", json=PAYLOAD, headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    async def test_create_with_key_succeeds(self, keyed) -> None:
        resp = await keyed.post("/api/v1/sources", json=PAYLOAD, headers={"X-API-Key": KEY})
        assert resp.status_code == 201

    async def test_delete_requires_key(self, keyed) -> None:
        """删源会丢掉水位游标，绝不能匿名。"""
        created = await keyed.post("/api/v1/sources", json=PAYLOAD, headers={"X-API-Key": KEY})
        source_id = created.json()["id"]

        assert (await keyed.delete(f"/api/v1/sources/{source_id}")).status_code == 401
        ok = await keyed.delete(f"/api/v1/sources/{source_id}", headers={"X-API-Key": KEY})
        assert ok.status_code == 204

    async def test_patch_requires_key(self, keyed) -> None:
        created = await keyed.post("/api/v1/sources", json=PAYLOAD, headers={"X-API-Key": KEY})
        source_id = created.json()["id"]
        resp = await keyed.patch(f"/api/v1/sources/{source_id}", json={"enabled": False})
        assert resp.status_code == 401

    async def test_trigger_collect_requires_key(self, keyed) -> None:
        created = await keyed.post("/api/v1/sources", json=PAYLOAD, headers={"X-API-Key": KEY})
        source_id = created.json()["id"]
        resp = await keyed.post(f"/api/v1/sources/{source_id}/collect")
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestClosedWhenUnconfigured:
    async def test_write_is_403_when_no_key_configured(self, keyless) -> None:
        """没配 key 就整体关闭，而不是默认放行。"""
        assert (await keyless.post("/api/v1/sources", json=PAYLOAD)).status_code == 403

    async def test_supplying_a_key_does_not_help(self, keyless) -> None:
        resp = await keyless.post("/api/v1/sources", json=PAYLOAD, headers={"X-API-Key": KEY})
        assert resp.status_code == 403


@pytest.mark.asyncio
class TestReadEndpointsStayOpen:
    """查询接口不上锁 —— 上锁的是写，不是读。"""

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/sources", "/api/v1/media", "/api/v1/resources", "/api/v1/stats", "/healthz"],
    )
    async def test_readable_without_key(self, keyless, path) -> None:
        assert (await keyless.get(path)).status_code == 200
