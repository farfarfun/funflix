"""归一后的作品实体。多条来源、多个网盘链接最终都挂到同一个 Media 上。"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from funflix.base.enums import MediaType, enum_col
from funflix.models.association import media_resource
from funflix.models.base import Base, JsonType, PkType, TimestampMixin, uuid7
from funflix.models.tag import Tag, media_tag

if TYPE_CHECKING:
    from funflix.models.resource import Resource
    from funflix.models.work import Work

#: year 为未知时写入的哨兵值。
#: 不能用 NULL —— SQLite 与 PostgreSQL 对唯一索引中 NULL 的判定不同（NULL != NULL），
#: 会导致"年份未知"的同名作品在 PG 上无限重复建行。
UNKNOWN_YEAR = 0

#: season 为"无季概念"时写入的值：电影、单季剧、综艺。
#: 和 `UNKNOWN_YEAR` 同样的理由不能用 NULL —— 它参与 `uq_media_season` 唯一索引。
#:
#: 注意它和"季号未知"是**同一个值**。规则抽不出季号时按第一季/无季处理，
#: 真有第二季的那条自然带着 `第2季`/`S02`/`年番2` 的写法，会落到 season=2。
NO_SEASON = 0


class Media(TimestampMixin, Base):
    """一部作品的**一季**。资源挂在这一层。

    `work_id` / `season` 才是它的身份，`norm_key` / `media_type` / `year`
    保留下来是为了调试与追溯（这一季自己的播出年份可能和作品首播年份不同），
    不再参与归并判定 —— 原因见 `models/work.py` 的模块说明。
    """

    __tablename__ = "media"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)

    #: 所属作品。迁移 B 之后 NOT NULL —— 一季必须属于某部作品。
    work_id: Mapped[uuid.UUID] = mapped_column(
        PkType, sa.ForeignKey("work.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 季号。0 = 无季概念，见 `NO_SEASON`。参与 `uq_media_season`，不能为 NULL。
    season: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=NO_SEASON)

    #: 展示用主标题（取首次见到的清洗后标题），如「大主宰 第2季」
    title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    #: 所属作品的归一键，由 services.normalize 的纯函数产出，见 docs/DESIGN.md §4.3。
    #: **不参与身份判定**（身份是 `(work_id, season)`），留着是为了排查时能
    #: 一眼看出这一季挂在哪个作品下，不用再 join 一次 work。
    norm_key: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    original_title: Mapped[str | None] = mapped_column(sa.Text)

    media_type: Mapped[MediaType] = mapped_column(
        enum_col(MediaType), nullable=False, default=MediaType.UNKNOWN
    )
    #: 0 表示年份未知，见 UNKNOWN_YEAR
    year: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=UNKNOWN_YEAR)

    #: 收集到的各种叫法（简繁、别名、带噪声的原始标题）
    aliases: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)

    # --- 预留的外部富化字段，M1 不填 ---
    tmdb_id: Mapped[int | None] = mapped_column(sa.Integer, index=True)
    douban_id: Mapped[str | None] = mapped_column(sa.String(32), index=True)
    imdb_id: Mapped[str | None] = mapped_column(sa.String(16), index=True)
    poster_url: Mapped[str | None] = mapped_column(sa.String(1024))
    overview: Mapped[str | None] = mapped_column(sa.Text)

    # --- 冗余计数，列表页避免 N+1 聚合 ---
    resource_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    valid_resource_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    work: Mapped[Work] = relationship(back_populates="seasons")
    resources: Mapped[list[Resource]] = relationship(
        secondary=media_resource, back_populates="media_list"
    )
    tags: Mapped[list[Tag]] = relationship(secondary=media_tag)

    __table_args__ = (
        # 身份只有 `(work_id, season)`。原来的 `uq_media_identity(norm_key,
        # media_type, year)` 在迁移 B 里删掉了 —— 它把 `media_type` 和 `year`
        # 塞进身份，于是同一部剧被判成 anime 和 tv 的两条分享会裂成两行，
        # 年份识别成 0 / 2023 / 2025 的又各裂一行。92 万行 media 里
        # 63 万行 year=0、42 万行 type=unknown，就是这么来的。
        sa.UniqueConstraint("work_id", "season", name="uq_media_season"),
        sa.Index("ix_media_title", "title"),
    )

    @property
    def year_or_none(self) -> int | None:
        """年份，未知时返回 None 而不是哨兵值 0。

        Returns:
            真实年份；`year` 等于 `UNKNOWN_YEAR` 时返回 None。对外输出（API、
            CLI 展示）一律用这个，别直接读 `year`，否则会显示成"0 年"。
        """
        return None if self.year == UNKNOWN_YEAR else self.year
