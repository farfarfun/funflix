"""网盘资源 —— 一条链接及其元信息。整个系统的核心表。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from funflix.base.enums import CheckStatus, Provider, Quality, enum_col
from funflix.models.association import media_resource
from funflix.models.base import Base, BigIntType, PkType, TimestampMixin, UTCDateTime, uuid7

if TYPE_CHECKING:
    from funflix.models.media import Media
    from funflix.models.raw import RawDocument


class Resource(TimestampMixin, Base):
    __tablename__ = "resource"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)

    #: 首次发现该资源的原始文本，用于溯源
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PkType, sa.ForeignKey("raw_document.id", ondelete="SET NULL")
    )

    # --- 链接本体 ---
    provider: Mapped[Provider] = mapped_column(enum_col(Provider), nullable=False)
    #: 从 URL 提取的分享标识。全局去重锚点是 (provider, share_id) 而非 url ——
    #: 同一份分享的 URL 写法可能不同（协议、子域名、追踪参数）。
    share_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    url: Mapped[str] = mapped_column(sa.String(2048), nullable=False)
    passcode: Mapped[str | None] = mapped_column(sa.String(32))

    # --- 资源描述 ---
    #: 原文中这条链接对应的标题片段，保留噪声，便于人工核对
    title_raw: Mapped[str | None] = mapped_column(sa.String(512))
    quality: Mapped[Quality] = mapped_column(
        enum_col(Quality), nullable=False, default=Quality.UNKNOWN
    )
    #: 如 "S01E01-E12" / "全40集"，格式不强约束
    episode_info: Mapped[str | None] = mapped_column(sa.String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigIntType)

    # --- 校验任务状态机 ---
    check_status: Mapped[CheckStatus] = mapped_column(
        enum_col(CheckStatus), nullable=False, default=CheckStatus.UNCHECKED
    )
    check_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 下次复查时间，由 check_status 决定 TTL，见 docs/DESIGN.md §6.4
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # --- 热度信号：同一链接被多处分享说明流传更广 ---
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    seen_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    #: 该链接里包含的全部作品。合集链接会关联多部。
    media_list: Mapped[list[Media]] = relationship(
        secondary=media_resource, back_populates="resources"
    )
    raw_document: Mapped[RawDocument | None] = relationship(back_populates="resources")
    #: 校验历史（LinkCheck）跟这张表没有外键，也没有 ORM 关系——它完全独立存储，
    #: 只认 (provider, share_id)，查历史时按这两列去 link_check 表里查，
    #: 见 models/check.py 顶部说明与 services/maintenance.py 的 relink_checks。

    __table_args__ = (
        sa.UniqueConstraint("provider", "share_id", name="uq_resource_provider_share"),
        # worker 领取待校验资源的主查询路径
        sa.Index("ix_resource_check_queue", "check_status", "next_check_at"),
        # 校验队列之外，按状态筛资源
        sa.Index("ix_resource_status", "check_status"),
    )
