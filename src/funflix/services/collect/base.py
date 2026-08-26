"""采集器抽象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from funflix.models import Source


@dataclass(slots=True, frozen=True)
class CollectedMessage:
    """从采集源拉到的一条消息。"""

    #: 源侧的消息 ID。必须在该源内单调递增，水位依赖这个性质。
    message_id: str
    text: str
    published_at: datetime | None = None
    url: str | None = None

    @property
    def numeric_id(self) -> int | None:
        return int(self.message_id) if self.message_id.isdigit() else None


@dataclass(slots=True)
class FetchResult:
    """一次采集的产出。"""

    messages: list[CollectedMessage] = field(default_factory=list)
    #: 实际翻了几页，用于判断是否因 max_pages 截断
    pages_fetched: int = 0
    #: True 表示还有更早的新消息没取完（撞到了 max_pages 上限）
    truncated: bool = False
    #: 采集源标题等元信息
    title: str | None = None
    #: 采集器自定义的水位状态，由 runner 合并进 `Source.extra` 持久化。
    #: 消息 ID 单调的源（如 Telegram）用不上它；
    #: 表格类源的行 ID 不单调，只能靠文档版本号之类的自定义状态判断"有没有更新"。
    state: dict[str, Any] = field(default_factory=dict)

    # --- 反向补历史 ---
    #: 本轮回溯到的新低水位。None 表示不更新。
    backfill_cursor: str | None = None
    #: 历史是否已补到头。置 True 后不再往前空跑。
    backfill_done: bool = False
    #: 又出现了没采到的历史内容，要求把补历史**重新打开**。
    #:
    #: `backfill_done` 只有单向的 False→True，没有回头路。表格类源会追加新行、
    #: 频道会有一段没翻完的区间 —— 这些都得让补历史重新启动，否则那批内容
    #: 永远采不到，而每轮采集还都显示成功。
    backfill_pending: bool = False


@runtime_checkable
class Collector(Protocol):
    """采集器接口。

    实现者只负责"把水位之后的消息取出来"，
    落库、去重、推进水位统一由 runner 处理。
    """

    name: str

    @staticmethod
    def normalize_identifier(url: str) -> str | None:
        """从 URL 提取该源的稳定标识；无法识别时返回 None。"""
        ...

    async def fetch(self, source: Source) -> FetchResult:
        """往后追新：拉取高水位之后的消息，按 ID 升序返回。"""
        ...

    async def backfill(self, source: Source) -> FetchResult:
        """往前补历史：拉取低水位之前的内容。

        只有高水位的话历史永远补不回来 —— 首次接入只取最新一页，
        更早的全部丢失。所以两个方向要分开推进：
        追新每轮都跑；补历史跑到拉不动为止，然后 `backfill_done=True` 收工。

        不支持回溯的采集器返回空结果并置 `backfill_done=True` 即可。
        """
        ...
