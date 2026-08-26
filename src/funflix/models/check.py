"""链接校验历史。只追加，不更新。

保留时序才能回答"这条链接什么时候挂的""某网盘最近整体失效率是不是异常"——
后者是判断"探针本身挂了"还是"链接真失效了"的关键信号。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from funflix.base.enums import CheckStatus, enum_col
from funflix.models.base import Base, PkType, UTCDateTime, utcnow

if TYPE_CHECKING:
    from funflix.models.resource import Resource


class LinkCheck(Base):
    __tablename__ = "link_check"

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        PkType, sa.ForeignKey("resource.id", ondelete="CASCADE"), nullable=False
    )

    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    status: Mapped[CheckStatus] = mapped_column(enum_col(CheckStatus), nullable=False)
    http_code: Mapped[int | None] = mapped_column(sa.Integer)
    #: 探针实现标识（如 "quark-anon-v1"），探针换实现后能区分历史数据
    probe: Mapped[str | None] = mapped_column(sa.String(64))
    detail: Mapped[str | None] = mapped_column(sa.Text)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)

    resource: Mapped[Resource] = relationship(back_populates="checks")

    __table_args__ = (sa.Index("ix_link_check_resource_time", "resource_id", "checked_at"),)
