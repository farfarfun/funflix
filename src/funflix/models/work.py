"""作品（剧集/系列）实体 —— 搜索的主体。

为什么在 `Media` 之上再加一层：`Media` 原本既代表"一部作品"又代表
"一条分享归一后的结果"，这两件事在真实语料里对不上。《大主宰》在分享
频道里有 448 条标题变体（第 N 集、4K高码、年番2、S02……），按
`(norm_key, media_type, year)` 认身份的话会落出几十行，搜出来全是重复。

拆开之后职责清晰：

- `Work` = 一部剧/系列。搜索只搜它，一部剧只出现一次。
- `Media` = 这部剧的**一季**。资源仍然挂在季上（第 1 季和第 2 季的链接
  不能混在一起）。

身份上最关键的变化：`Work` 的身份**只有 `norm_key`**，不含 year 和
media_type。生产库里 63 万行 year=0、42 万行 type=unknown，把它们放进
身份就等于让同一部剧按"这条分享有没有写年份、类型猜成了什么"裂开。
年份和类型降级成普通属性 —— 取已知的最佳值，不参与归并判定。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from funflix.base.enums import MediaType, enum_col
from funflix.models.base import Base, JsonType, PkType, TimestampMixin, uuid7
from funflix.models.media import UNKNOWN_YEAR

if TYPE_CHECKING:
    from funflix.models.media import Media


class Work(TimestampMixin, Base):
    __tablename__ = "work"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)

    #: 展示用作品名，如「大主宰」。不带季号、不带集数、不带画质。
    title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    #: 作品归一键，由 `services.text.normalize.series_norm_key` 产出。
    #: **这是 Work 的唯一身份** —— 两条资源的 series_norm_key 相同就是同一部剧。
    norm_key: Mapped[str] = mapped_column(sa.String(500), nullable=False, unique=True)
    original_title: Mapped[str | None] = mapped_column(sa.Text)

    #: 收集到的各种叫法（简繁、别名、带噪声的原始标题）。
    #: 归并时把被合并方的标题并进来，保留可追溯性。
    aliases: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)

    media_type: Mapped[MediaType] = mapped_column(
        enum_col(MediaType), nullable=False, default=MediaType.UNKNOWN
    )
    #: 首播年份。0 表示未知，见 `UNKNOWN_YEAR`。
    #: 和 media_type 一样**不参与身份判定**，只是取已知的最佳值。
    year: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=UNKNOWN_YEAR)

    # --- 预留的外部富化字段 ---
    tmdb_id: Mapped[int | None] = mapped_column(sa.Integer, index=True)
    douban_id: Mapped[str | None] = mapped_column(sa.String(32), index=True)
    imdb_id: Mapped[str | None] = mapped_column(sa.String(16), index=True)
    poster_url: Mapped[str | None] = mapped_column(sa.String(1024))
    overview: Mapped[str | None] = mapped_column(sa.Text)

    # --- 冗余计数，列表页避免 N+1 聚合。重算而非增减，见 services/counters.py ---
    season_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    resource_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    valid_resource_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    seasons: Mapped[list[Media]] = relationship(
        back_populates="work",
        order_by="Media.season",
        # 作品被删时季跟着删；资源挂在 media_resource 上，由清理任务另行处理
        cascade="all, delete-orphan",
    )

    __table_args__ = (sa.Index("ix_work_title", "title"),)

    @property
    def year_or_none(self) -> int | None:
        return None if self.year == UNKNOWN_YEAR else self.year
