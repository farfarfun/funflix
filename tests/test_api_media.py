"""作品 / 资源 / 统计查询接口。"""

from __future__ import annotations

import pytest
import pytest_asyncio

from funflix.base.enums import CheckStatus, MediaType, Provider, Quality
from funflix.models import Media, Resource, utcnow
from funflix.services.counters import refresh_media_counters


def _media(title: str, norm: str, *, media_type=MediaType.MOVIE, year: int = 2024) -> Media:
    return Media(
        title=title,
        norm_key=norm,
        media_type=media_type,
        year=year,
        aliases=[],
    )


def _resource(share_id: str, *, provider=Provider.QUARK, status=CheckStatus.VALID) -> Resource:
    now = utcnow()
    return Resource(
        provider=provider,
        share_id=share_id,
        url=f"https://pan.quark.cn/s/{share_id}",
        quality=Quality.FHD_1080P,
        check_status=status,
        first_seen_at=now,
        last_seen_at=now,
    )


@pytest_asyncio.fixture
async def seeded(session):
    """两部作品：一部带一条有效资源，一部没有资源。

    计数走 `refresh_media_counters` 真实算一遍，而不是手工赋值 ——
    手工赋值会把「生产代码从不维护这两个计数」这件事整个盖住。
    """
    hit = _media("误杀2", "误杀2")
    hit.resources = [_resource("aaa111")]

    barren = _media("流浪地球", "流浪地球", media_type=MediaType.TV, year=2019)

    # 年份未知的哨兵值，用来验证出参会被抹成 null
    unknown = _media("无名剧", "无名剧", year=0)

    session.add_all([hit, barren, unknown])
    await session.commit()
    await refresh_media_counters(session, [hit.id, barren.id, unknown.id])
    await session.commit()
    return {"hit": hit, "barren": barren, "unknown": unknown}


@pytest.mark.asyncio
class TestListMedia:
    async def test_lists_all_with_total(self, client, seeded) -> None:
        body = (await client.get("/api/v1/media")).json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        assert body["page"] == 1

    async def test_keyword_narrows_results(self, client, seeded) -> None:
        body = (await client.get("/api/v1/media", params={"keyword": "误杀"})).json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "误杀2"

    async def test_filters_by_media_type(self, client, seeded) -> None:
        body = (await client.get("/api/v1/media", params={"media_type": "tv"})).json()
        assert [i["title"] for i in body["items"]] == ["流浪地球"]

    async def test_valid_only_drops_media_without_valid_resource(self, client, seeded) -> None:
        body = (await client.get("/api/v1/media", params={"valid_only": True})).json()
        assert [i["title"] for i in body["items"]] == ["误杀2"]

    async def test_total_reflects_filter_not_page_size(self, client, seeded) -> None:
        """total 必须是全量匹配数，不能是当前页条数 —— 否则前端翻页器算错页数。"""
        body = (await client.get("/api/v1/media", params={"size": 1})).json()
        assert body["total"] == 3
        assert len(body["items"]) == 1

    async def test_paginates(self, client, seeded) -> None:
        first = (await client.get("/api/v1/media", params={"size": 2, "page": 1})).json()
        second = (await client.get("/api/v1/media", params={"size": 2, "page": 2})).json()
        assert len(first["items"]) == 2
        assert len(second["items"]) == 1
        ids = {i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}
        assert len(ids) == 3

    async def test_unknown_year_serialized_as_null(self, client, seeded) -> None:
        body = (await client.get("/api/v1/media", params={"keyword": "无名剧"})).json()
        assert body["items"][0]["year"] is None

    async def test_unknown_year_is_queryable(self, client, seeded) -> None:
        """出参把哨兵年份吐成 null，入参就得能查回来。

        之前 year 限了 ge=1888，客户端从列表里看到 year=null 这一档，
        想下钻却没有任何取值查得到 —— 出参吐出一个入参拒收的值。
        """
        body = (await client.get("/api/v1/media", params={"year": 0})).json()
        assert [i["title"] for i in body["items"]] == ["无名剧"]

    @pytest.mark.parametrize("keyword", ["%", "_", "%%"])
    async def test_sql_wildcards_are_literal_not_patterns(self, client, seeded, keyword) -> None:
        """关键词里的 % 和 _ 必须当字面量。

        不转义的话搜 `%` 命中全表、搜 `_` 匹配任意单字符，
        而分享标题里 `S01_1080p`、`100%纯爱` 这类名字很常见。
        """
        body = (await client.get("/api/v1/media", params={"keyword": keyword})).json()
        assert body["total"] == 0
        assert body["items"] == []

    async def test_literal_percent_in_title_is_findable(self, client, session, seeded) -> None:
        session.add(_media("100%纯爱", "100纯爱"))
        await session.commit()
        body = (await client.get("/api/v1/media", params={"keyword": "100%纯"})).json()
        assert [i["title"] for i in body["items"]] == ["100%纯爱"]

    async def test_huge_page_is_rejected_not_500(self, client, seeded) -> None:
        """page 没有上限时 (page-1)*size 会溢出，驱动抛 OverflowError 变成 500。"""
        resp = await client.get("/api/v1/media", params={"page": 10**19})
        assert resp.status_code == 422

    async def test_page_past_end_is_empty_not_error(self, client, seeded) -> None:
        body = (await client.get("/api/v1/media", params={"page": 999})).json()
        assert body["items"] == []
        assert body["total"] == 3


@pytest.mark.asyncio
class TestResourceCounters:
    """`resource_count` / `valid_resource_count` 必须由生产代码维护。

    这两个字段是列表页的冗余计数（DESIGN §3.3）。valid_resource_count 一度
    从没有任何写入点，接口对每一行都返回 0 —— 同一份响应既说「这些都有可用
    链接」（valid_only 过滤用 EXISTS 实时算）又说「一条可用链接都没有」。
    """

    async def test_counts_reflect_linked_resources(self, client, seeded) -> None:
        body = (await client.get("/api/v1/media", params={"keyword": "误杀"})).json()
        assert body["items"][0]["resource_count"] == 1
        assert body["items"][0]["valid_resource_count"] == 1

    async def test_media_without_resources_counts_zero(self, client, seeded) -> None:
        body = (await client.get("/api/v1/media", params={"keyword": "流浪地球"})).json()
        assert body["items"][0]["resource_count"] == 0
        assert body["items"][0]["valid_resource_count"] == 0

    async def test_valid_count_drops_when_link_dies(self, client, session, seeded) -> None:
        """链接被校验成失效后，作品的有效计数要跟着降下来。"""
        from funflix.services.verify.base import CheckOutcome
        from funflix.services.verify.runner import check_resource

        class DeadProbe:
            name, provider, needs_auth = "stub", Provider.QUARK, False

            async def check(self, ref):
                return CheckOutcome(status=CheckStatus.INVALID, http_code=404)

        resource = seeded["hit"].resources[0]
        await check_resource(session, resource, DeadProbe())
        await session.commit()

        body = (await client.get("/api/v1/media", params={"keyword": "误杀"})).json()
        assert body["items"][0]["resource_count"] == 1, "链接还在，总数不该变"
        assert body["items"][0]["valid_resource_count"] == 0, "但它已经不可用了"


@pytest.mark.asyncio
class TestGetMedia:
    async def test_returns_detail_with_resources(self, client, seeded) -> None:
        resp = await client.get(f"/api/v1/media/{seeded['hit'].id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "误杀2"
        assert len(body["resources"]) == 1
        assert body["resources"][0]["provider"] == "quark"
        assert body["resources"][0]["check_status"] == "valid"
        assert body["tags"] == []

    async def test_404_for_missing(self, client, seeded) -> None:
        assert (await client.get("/api/v1/media/99999")).status_code == 404


@pytest.mark.asyncio
class TestListResources:
    async def test_lists_resources(self, client, seeded) -> None:
        body = (await client.get("/api/v1/resources")).json()
        assert body["total"] == 1
        assert body["items"][0]["url"].endswith("aaa111")

    async def test_filters_by_check_status(self, client, seeded) -> None:
        assert (await client.get("/api/v1/resources", params={"check_status": "invalid"})).json()[
            "total"
        ] == 0

    async def test_filters_by_provider(self, client, seeded) -> None:
        body = (await client.get("/api/v1/resources", params={"provider": "alipan"})).json()
        assert body["total"] == 0

    async def test_404_for_missing(self, client, seeded) -> None:
        assert (await client.get("/api/v1/resources/99999")).status_code == 404


@pytest.mark.asyncio
class TestStats:
    async def test_reports_pipeline_counts(self, client, seeded) -> None:
        body = (await client.get("/api/v1/stats")).json()
        assert body["media_total"] == 3
        assert body["resource_total"] == 1
        assert body["media_by_type"]["movie"] == 2
        assert body["media_by_type"]["tv"] == 1
        assert body["resource_by_check"]["valid"] == 1
        assert body["resource_by_provider"]["quark"] == 1
        assert body["media_resource_total"] == 1

    async def test_counts_orphan_resources(self, client, session, seeded) -> None:
        """没挂到任何作品上的资源要被单独统计出来，否则数据丢失是无声的。"""
        session.add(_resource("orphan1"))
        await session.commit()
        body = (await client.get("/api/v1/stats")).json()
        assert body["resource_orphan"] == 1
        assert body["resource_total"] == 2

    async def test_empty_db_returns_zeros(self, client) -> None:
        body = (await client.get("/api/v1/stats")).json()
        assert body["media_total"] == 0
        assert body["raw_by_status"] == {}
