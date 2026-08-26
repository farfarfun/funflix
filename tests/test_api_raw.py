from __future__ import annotations

import pytest


@pytest.mark.asyncio
class TestCreateRawDocument:
    async def test_creates_and_returns_201(self, client) -> None:
        resp = await client.post(
            "/api/v1/raw",
            json={
                "content": "剧名A\nhttps://example.com/s/abc",
                "source_type": "telegram",
                "source_name": "某频道",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["duplicated"] is False
        assert body["parse_status"] == "pending"
        assert len(body["content_hash"]) == 64

    async def test_second_submit_reports_duplicated(self, client) -> None:
        payload = {"content": "剧名A\n链接x"}
        first = (await client.post("/api/v1/raw", json=payload)).json()
        second = (await client.post("/api/v1/raw", json=payload)).json()

        assert second["duplicated"] is True
        assert second["id"] == first["id"]

    async def test_rejects_empty_content(self, client) -> None:
        resp = await client.post("/api/v1/raw", json={"content": ""})
        assert resp.status_code == 422

    async def test_rejects_oversized_content(self, client) -> None:
        resp = await client.post("/api/v1/raw", json={"content": "x" * 100_001})
        assert resp.status_code == 413


@pytest.mark.asyncio
class TestBulkCreate:
    async def test_counts_created_and_duplicated(self, client) -> None:
        resp = await client.post(
            "/api/v1/raw/bulk",
            json={
                "items": [
                    {"content": "剧名A"},
                    {"content": "剧名B"},
                    {"content": "剧名A"},
                ]
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert (body["total"], body["created"], body["duplicated"]) == (3, 2, 1)


@pytest.mark.asyncio
class TestQueryRawDocument:
    async def test_get_by_id_returns_full_content(self, client) -> None:
        created = (await client.post("/api/v1/raw", json={"content": "剧名A\n链接x"})).json()
        resp = await client.get(f"/api/v1/raw/{created['id']}")

        assert resp.status_code == 200
        assert resp.json()["content"] == "剧名A\n链接x"

    async def test_get_missing_returns_404(self, client) -> None:
        assert (await client.get("/api/v1/raw/999999")).status_code == 404

    async def test_list_filters_by_status(self, client) -> None:
        await client.post("/api/v1/raw", json={"content": "剧名A"})
        await client.post("/api/v1/raw", json={"content": "剧名B"})

        resp = await client.get("/api/v1/raw", params={"parse_status": "pending"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        # 列表项不含全文
        assert "content" not in body["items"][0]

        assert (await client.get("/api/v1/raw", params={"parse_status": "done"})).json()[
            "total"
        ] == 0

    async def test_list_paginates(self, client) -> None:
        for i in range(5):
            await client.post("/api/v1/raw", json={"content": f"剧名{i}"})

        body = (await client.get("/api/v1/raw", params={"page": 2, "size": 2})).json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
