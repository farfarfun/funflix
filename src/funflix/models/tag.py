"""标签与作品↔标签关联。

分类筛选是资源站的主干导航（「悬疑」「古装」「2024」「国产」）。
`media_type` 太粗（只有 movie/tv/anime/variety/documentary），撑不起筛选。

标签按 `kind` 分组而不是拆成多张表：题材、地区、语言、来源渠道的结构完全一样，
拆表只会让每加一个维度就要改一次 schema。
"""

from __future__ import annotations

import uuid
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from funflix.base.enums import enum_col
from funflix.models.base import Base, PkType, TimestampMixin, UTCDateTime, utcnow, uuid7


class TagKind(StrEnum):
    """标签维度。"""

    GENRE = "genre"  # 题材：悬疑 / 喜剧 / 古装
    REGION = "region"  # 地区：国产 / 美国 / 日本
    LANGUAGE = "language"  # 语言：国语 / 粤语
    YEAR = "year"  # 年代：2024 / 90年代
    OTHER = "other"


class Tag(TimestampMixin, Base):
    __tablename__ = "tag"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)
    kind: Mapped[TagKind] = mapped_column(enum_col(TagKind), nullable=False, default=TagKind.OTHER)
    #: 展示名
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: 归一后的匹配键（去空白标点、小写）。「科幻」「科 幻」应收敛到一起。
    norm_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: 冗余计数，列表页排序用
    media_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    __table_args__ = (
        sa.UniqueConstraint("kind", "norm_key", name="uq_tag_kind_key"),
        sa.Index("ix_tag_kind_count", "kind", "media_count"),
    )


media_tag = sa.Table(
    "media_tag",
    Base.metadata,
    sa.Column("media_id", PkType, sa.ForeignKey("media.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("tag_id", PkType, sa.ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("created_at", UTCDateTime, nullable=False, default=utcnow),
    # 反向查询「这个标签下有哪些作品」—— 筛选页的主查询路径
    sa.Index("ix_media_tag_tag", "tag_id"),
)
