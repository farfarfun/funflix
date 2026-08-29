"""采集源 —— 流水线最前置的一层。

一个 Source 是一个可持续拉取的消息流（如一个 Telegram 频道）。
它持有**水位（watermark）**，每次采集只取水位之后的新消息，
把消息正文写成 RawDocument 后即结束职责，解析交给下游任务。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from funflix.base.enums import SourceType, enum_col
from funflix.models.base import Base, JsonType, PkType, TimestampMixin, UTCDateTime, uuid7

if TYPE_CHECKING:
    from funflix.models.raw import RawDocument


class Source(TimestampMixin, Base):
    __tablename__ = "source"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)

    source_type: Mapped[SourceType] = mapped_column(enum_col(SourceType), nullable=False)
    #: 采集源地址，如 https://t.me/s/Quark_Movies
    url: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    #: 规范化后的唯一标识，如 Telegram 的频道名 "Quark_Movies"。
    #: 同一频道有多种 URL 写法（t.me/x、t.me/s/x、@x），靠它做唯一性判定而非 url。
    identifier: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(sa.String(255))

    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    fetch_interval_seconds: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=900)
    #: 单次采集最多回溯几页，防止首次接入或长期停机后无限翻页
    max_pages_per_fetch: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=5)

    # --- 水位 ---
    #: 主水位：已采集到的最大消息 ID。
    #: 用 ID 而不是时间做主水位 —— ID 单调且精确，不受时钟漂移、
    #: 同秒多条消息、以及消息编辑导致时间戳变化的影响。
    cursor_message_id: Mapped[str | None] = mapped_column(sa.String(64))
    #: 辅助水位：对应消息的发布时间。仅用于展示与人工核对。
    cursor_published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # --- 反向补历史 ---
    # 只有高水位的话，历史永远补不回来：首次接入只取最新一页，更早的全部丢失。
    # 所以再设一个低水位，每轮朝两个方向各拉若干页。
    #: 低水位：已回溯到的最早消息 ID。往前补历史从这里继续。
    backfill_cursor_id: Mapped[str | None] = mapped_column(sa.String(64))
    #: 历史是否已补完。到顶后置 True，此后每轮只追新，不再往前空跑。
    backfill_done: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    #: 累计回溯到的条数，用于观察补历史的进度
    total_backfilled: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    # --- 调度与健康度 ---
    last_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_fetch_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    #: 累计产出的新 RawDocument 数（不含被 content_hash 去重掉的）
    total_collected: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    extra: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)

    documents: Mapped[list[RawDocument]] = relationship(back_populates="source")

    def reset_watermark(self) -> None:
        """把水位与健康度归零，保留身份与调度配置。

        清空原始文本后必须调一次：水位还留着的话，采集器会认为"都采过了"，
        重建后一条也拉不回来。

        方法放在模型上而不是维护脚本里，是因为它要枚举本表的一批列 ——
        放在这里，加字段的人改的就是同一个文件，漏掉的概率小得多。
        """
        self.cursor_message_id = None
        self.cursor_published_at = None
        self.backfill_cursor_id = None
        self.backfill_done = False
        self.total_collected = 0
        self.total_backfilled = 0
        self.last_error = None
        self.consecutive_failures = 0
        self.next_fetch_at = None
        self.last_fetched_at = None
        self.last_success_at = None
        self.lease_until = None
        # 采集器自定义水位（如文档版本号）也在这里，不清就会跟主水位一起卡住
        self.extra = {}

    __table_args__ = (
        sa.UniqueConstraint("source_type", "identifier", name="uq_source_type_identifier"),
        sa.Index("ix_source_fetch_queue", "enabled", "next_fetch_at"),
    )
