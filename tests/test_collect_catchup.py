"""长时间停机后的追赶采集。

采集器是**从最新往回翻页**的，翻到水位就停，页数用完则报 `truncated=True`。
停机一天后频道积了几千条，一轮翻不完 —— 这时候水位怎么走，决定了中间那批
消息是被补上还是被永久跳过。

一个标量水位表达不了「最新 100 条已采、中间 1900 条待采」，所以这里用两个
额外的状态：`pending_cursor`（追赶完成后该落到哪）与 `catchup_before`
（下一轮从哪一页接着往回翻）。
"""

from __future__ import annotations

import pytest

from funflix.base.enums import SourceType
from funflix.models import Source
from funflix.services.collect.base import CollectedMessage, FetchResult
from funflix.services.collect.runner import collect_source


def _source(**kwargs) -> Source:
    defaults = dict(
        source_type=SourceType.TELEGRAM,
        url="https://t.me/s/demo",
        identifier="demo",
        enabled=True,
        extra={},
        max_pages_per_fetch=5,
        backfill_done=True,  # 历史早就补完了，不会有 backfill 兜底
    )
    return Source(**{**defaults, **kwargs})


def _msg(i: int) -> CollectedMessage:
    return CollectedMessage(
        message_id=str(i), text=f"名称：剧集{i}\n链接：https://pan.quark.cn/s/x{i:06d}"
    )


class _Collector:
    """按脚本返回结果的假采集器，记录每轮拿到的 source.extra。"""

    name = "fake"

    def __init__(self, results: list[FetchResult]) -> None:
        self._results = list(results)
        self.seen_extra: list[dict] = []

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        return "demo"

    async def fetch(self, source: Source) -> FetchResult:
        self.seen_extra.append(dict(source.extra))
        return self._results.pop(0)

    async def backfill(self, source: Source) -> FetchResult:
        return FetchResult(backfill_done=True)


@pytest.mark.asyncio
class TestTruncatedCatchup:
    async def test_truncated_round_does_not_jump_the_watermark(self, session) -> None:
        """核心：页数用完时，水位不能直接跳到最新那条。

        跳过去的话，下一轮从新水位往回看会立刻碰到已见过的 ID 就停，
        中间那批消息再也取不到 —— 而 backfill_done 已经是 True，
        补历史也不会启动。停机一天 = 静默丢掉一天的消息。
        """
        source = _source(cursor_message_id="1000")
        session.add(source)
        await session.commit()

        collector = _Collector(
            [FetchResult(messages=[_msg(i) for i in range(1441, 1501)], truncated=True)]
        )
        await collect_source(session, source, collector)

        assert source.cursor_message_id == "1000", "页数没用完之前不该推进水位"

    async def test_catchup_completes_then_watermark_lands_on_the_newest(self, session) -> None:
        """追赶跑完后，水位要落到整段追赶里见过的最大 ID，而不是最后一轮的最大 ID。"""
        source = _source(cursor_message_id="1000")
        session.add(source)
        await session.commit()

        collector = _Collector(
            [
                # 第一轮：取到 1441..1500，还没翻到水位
                FetchResult(messages=[_msg(i) for i in range(1441, 1501)], truncated=True),
                # 第二轮：接着往回翻，终于跨过水位
                FetchResult(messages=[_msg(i) for i in range(1001, 1441)], truncated=False),
            ]
        )
        await collect_source(session, source, collector)
        assert source.cursor_message_id == "1000", "第一轮还在追赶中"

        await collect_source(session, source, collector)
        assert source.cursor_message_id == "1500", (
            "追赶结束后水位应落在 1500（整段见过的最大），"
            f"实际 {source.cursor_message_id} —— 落在最后一轮的最大值就会把 1441..1500 重采一遍"
        )

    async def test_collector_is_told_where_to_resume(self, session) -> None:
        """采集器下一轮要知道从哪一页接着往回翻，否则每轮都从最新页重来、永远原地踏步。"""
        source = _source(cursor_message_id="1000")
        session.add(source)
        await session.commit()

        collector = _Collector(
            [
                FetchResult(
                    messages=[_msg(i) for i in range(1441, 1501)],
                    truncated=True,
                    state={"catchup_before": "1441"},
                ),
                FetchResult(messages=[_msg(i) for i in range(1001, 1441)], truncated=False),
            ]
        )
        await collect_source(session, source, collector)
        await collect_source(session, source, collector)

        assert collector.seen_extra[1].get("catchup_before") == "1441", (
            "第二轮开始时采集器应当能从 extra 里读到续翻位置"
        )

    async def test_state_is_cleared_after_catchup(self, session) -> None:
        """追赶状态用完要清掉，否则下一次正常采集会从一个陈旧的位置开始翻。"""
        source = _source(cursor_message_id="1000")
        session.add(source)
        await session.commit()

        collector = _Collector(
            [
                FetchResult(messages=[_msg(i) for i in range(1441, 1501)], truncated=True),
                FetchResult(messages=[_msg(i) for i in range(1001, 1441)], truncated=False),
            ]
        )
        await collect_source(session, source, collector)
        await collect_source(session, source, collector)

        assert not source.extra.get("pending_cursor"), "追赶完成后 pending_cursor 应被清掉"

    async def test_normal_round_advances_immediately(self, session) -> None:
        """没被截断的普通一轮，行为不变：水位照常推进到最新。"""
        source = _source(cursor_message_id="1000")
        session.add(source)
        await session.commit()

        collector = _Collector([FetchResult(messages=[_msg(1001), _msg(1002)], truncated=False)])
        await collect_source(session, source, collector)

        assert source.cursor_message_id == "1002"
