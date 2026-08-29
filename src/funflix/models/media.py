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

#: year 为未知时写入的哨兵值。
#: 不能用 NULL —— SQLite 与 PostgreSQL 对唯一索引中 NULL 的判定不同（NULL != NULL），
#: 会导致"年份未知"的同名作品在 PG 上无限重复建行。
UNKNOWN_YEAR = 0


class Media(TimestampMixin, Base):
    __tablename__ = "media"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)

    #: 展示用主标题（取首次见到的清洗后标题）
    title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    #: 归一键，由 services.normalize 的纯函数产出，见 docs/DESIGN.md §4.3
    #: 长度上限低于 title——它参与 uq_media_identity 唯一索引，不能无限放大。
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

    resources: Mapped[list[Resource]] = relationship(
        secondary=media_resource, back_populates="media_list"
    )
    tags: Mapped[list[Tag]] = relationship(secondary=media_tag)

    __table_args__ = (
        sa.UniqueConstraint("norm_key", "media_type", "year", name="uq_media_identity"),
        sa.Index("ix_media_title", "title"),
    )

    @property
    def year_or_none(self) -> int | None:
        return None if self.year == UNKNOWN_YEAR else self.year
