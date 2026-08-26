"""智能表格的行偏移推进。

表格的「低水位」是每个 sheet 已扫到的行偏移，backfill 靠它一片片往后推。
偏移一旦被写回一个更小的值，中间那段行就再也扫不到 —— 而 `backfill_done`
如果同时还是 True，补历史连启动都不会启动。
"""

from __future__ import annotations

import pytest

from funflix.services.collect.tencent_sheet import _CHUNK_SIZE, _OFFSET_KEY, _TOTAL_KEY
from funflix.services.collect.tencent_sheet import TencentSheetCollector as Sheet


class TestOffsetNeverGoesBackwards:
    def test_merge_keeps_the_larger_offset(self) -> None:
        """backfill 推到 30000 之后，一次只读了一页的 fetch 不能把它打回 120。

        文档追加新行后 `ver` 变化，fetch 会重扫该 sheet；它只读了第一片，
        若直接用页数算偏移就会写回 120，把 backfill 几百次请求的进度抹掉。
        之后 backfill 从 120 重扫到 30000，全程被 content_hash 去重，
        白跑几百次请求；期间新追加的行一直取不到。
        """
        merged = Sheet._merge_offset(existing=30000, consumed=_CHUNK_SIZE * 2)
        assert merged == 30000

    def test_merge_takes_progress_when_it_is_larger(self) -> None:
        merged = Sheet._merge_offset(existing=120, consumed=_CHUNK_SIZE * 5)
        assert merged == _CHUNK_SIZE * 5

    def test_merge_handles_missing_existing(self) -> None:
        assert Sheet._merge_offset(existing=None, consumed=180) == 180


class TestBackfillReopens:
    """新行追加进来时，补历史必须能重新启动。

    `backfill_done` 全仓库只有 `db reset` 会改回 False。文档追加 5000 行后
    偏移远小于总行数，但补历史因为 done=True 直接 return，新行永远采不到。
    """

    def test_pending_rows_reopen_backfill(self) -> None:
        assert Sheet._has_pending_rows(offsets={"s1": 120}, totals={"s1": 35000}) is True

    def test_no_pending_rows_leaves_it_closed(self) -> None:
        assert Sheet._has_pending_rows(offsets={"s1": 35000}, totals={"s1": 35000}) is False

    def test_unknown_total_is_not_pending(self) -> None:
        """总行数没拿到时不要瞎重开 —— 那会让补历史每轮空跑。"""
        assert Sheet._has_pending_rows(offsets={"s1": 120}, totals={}) is False


@pytest.mark.asyncio
class TestRunnerReopensBackfill:
    async def test_backfill_done_is_cleared_when_more_rows_appear(self, session) -> None:
        from funflix.base.enums import SourceType
        from funflix.models import Source
        from funflix.services.collect.base import FetchResult
        from funflix.services.collect.runner import collect_source

        source = Source(
            source_type=SourceType.TENCENT_DOCS,
            url="https://docs.qq.com/smartsheet/DT0abc",
            identifier="DT0abc",
            enabled=True,
            extra={_OFFSET_KEY: {"s1": 120}, _TOTAL_KEY: {"s1": 35000}},
            backfill_done=True,
        )
        session.add(source)
        await session.commit()

        class _C:
            name = "fake"

            @staticmethod
            def normalize_identifier(url):
                return "DT0abc"

            async def fetch(self, src):
                return FetchResult(backfill_pending=True)

            async def backfill(self, src):
                return FetchResult()

        await collect_source(session, source, _C())
        assert source.backfill_done is False, "还有没扫到的行，补历史必须重新打开"


@pytest.mark.asyncio
class TestBackfillResolvesColumnNames:
    """补历史必须自己去取一次列定义。

    列定义只随第 0 片下发，非 0 偏移的片一片都不带（实测 offset=60/120
    都是 0 个列定义）。追新天然从第 0 片开始所以没事；补历史是从存下来的
    偏移接着往下扫的，不单独取一次就永远拿不到列名，渲染出来每行都是
    `fn99gF：https://...` 这种原始字段 ID —— 抽取器认不出标题列，
    链接全部变成「未归属」。线上 482 条未归属资源全部出自这条路径。
    """

    async def test_columns_are_fetched_for_backfilled_chunks(self, session) -> None:
        import base64
        import json
        import zlib

        import httpx

        from funflix.base.enums import SourceType
        from funflix.models import Source
        from funflix.services.collect.tencent_sheet import _OFFSET_KEY as OK
        from funflix.services.collect.tencent_sheet import _TOTAL_KEY as TK
        from funflix.services.collect.tencent_sheet import TencentSheetCollector

        seen_offsets: list[int] = []

        def _payload(start: int) -> dict:
            """第 0 片带列定义，其余片只有行 —— 与线上实测行为一致。"""
            ops: list = [{"c": {"k2": {"k1": {f"r{start}": {"k1": {"fAAA": "值"}}}}}}]
            if start == 0:
                ops.append({"c": {"k3": {"k3": {"fAAA": {"k30": "剧名"}}}}})
            blob = base64.urlsafe_b64encode(zlib.compress(json.dumps(ops).encode())).decode()
            return {
                "clientVars": {
                    "collab_client_vars": {
                        "initialAttributedText": {"text": [{"smartsheet": blob}]}
                    }
                }
            }

        def handler(request: httpx.Request) -> httpx.Response:
            start = int(request.url.params.get("startRow", 0))
            seen_offsets.append(start)
            return httpx.Response(200, json=_payload(start))

        collector = TencentSheetCollector(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), chunk_delay=0
        )
        source = Source(
            source_type=SourceType.TENCENT_DOCS,
            url="https://docs.qq.com/smartsheet/DT0abc",
            identifier="DT0abc",
            enabled=True,
            extra={OK: {"s1": 120}, TK: {"s1": 600}},
            backfill_pages_per_fetch=2,
        )
        session.add(source)
        await session.commit()

        await collector.backfill(source)

        assert 0 in seen_offsets, (
            f"补历史没有去取第 0 片的列定义，请求过的偏移={seen_offsets} —— "
            "拿不到列名，每一行都会渲染成原始字段 ID"
        )

    async def test_backfilled_rows_render_with_real_column_names(self, session) -> None:
        """终点判据：落库的文本里是「剧名：」而不是「fAAA：」。"""
        import base64
        import json
        import zlib

        import httpx

        from funflix.base.enums import SourceType
        from funflix.models import Source
        from funflix.services.collect.tencent_sheet import _OFFSET_KEY as OK
        from funflix.services.collect.tencent_sheet import _TOTAL_KEY as TK
        from funflix.services.collect.tencent_sheet import TencentSheetCollector

        def _payload(start: int) -> dict:
            ops: list = [{"c": {"k2": {"k1": {f"r{start}": {"k1": {"fAAA": "某剧"}}}}}}]
            if start == 0:
                ops.append({"c": {"k3": {"k3": {"fAAA": {"k30": "剧名"}}}}})
            blob = base64.urlsafe_b64encode(zlib.compress(json.dumps(ops).encode())).decode()
            return {
                "clientVars": {
                    "collab_client_vars": {
                        "initialAttributedText": {"text": [{"smartsheet": blob}]}
                    }
                }
            }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_payload(int(request.url.params.get("startRow", 0))))

        collector = TencentSheetCollector(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), chunk_delay=0
        )
        source = Source(
            source_type=SourceType.TENCENT_DOCS,
            url="https://docs.qq.com/smartsheet/DT0abc",
            identifier="DT0abc",
            enabled=True,
            extra={OK: {"s1": 120}, TK: {"s1": 600}},
            backfill_pages_per_fetch=2,
        )
        session.add(source)
        await session.commit()

        result = await collector.backfill(source)
        assert result.messages
        assert "剧名：" in result.messages[0].text, result.messages[0].text
