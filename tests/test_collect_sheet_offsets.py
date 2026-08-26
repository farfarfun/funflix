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
