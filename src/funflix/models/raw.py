"""原始文本。整条流水线的入口与溯源根节点。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from funflix.base.enums import ParseStatus, SourceType, enum_col
from funflix.models.base import Base, JsonType, PkType, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from funflix.models.extraction import Extraction
    from funflix.models.resource import Resource
    from funflix.models.source import Source


class RawDocument(TimestampMixin, Base):
    """一条未经加工的分享文本。

    `content` 永远保持原样 —— 解析逻辑会迭代，原文是唯一能重跑的依据。
    """

    __tablename__ = "raw_document"

    id: Mapped[int] = mapped_column(PkType, primary_key=True)

    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: sha256(规范化后的 content)。入口去重锚点，挡住重复提交带来的 LLM 开销。
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)

    # --- 来源 ---
    #: 采集源。手工提交的文档没有 source，故可空。
    source_id: Mapped[int | None] = mapped_column(
        PkType, sa.ForeignKey("source.id", ondelete="SET NULL")
    )
    source_type: Mapped[SourceType] = mapped_column(
        enum_col(SourceType), nullable=False, default=SourceType.UNKNOWN
    )
    source_name: Mapped[str | None] = mapped_column(sa.String(128))
    source_url: Mapped[str | None] = mapped_column(sa.String(1024))
    source_msg_id: Mapped[str | None] = mapped_column(sa.String(128))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: 来源侧的任意附加元信息，不参与查询语义
    extra: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)

    # --- 解析任务状态机（见 docs/DESIGN.md §5）---
    parse_status: Mapped[ParseStatus] = mapped_column(
        enum_col(ParseStatus), nullable=False, default=ParseStatus.PENDING
    )
    parse_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    parse_error: Mapped[str | None] = mapped_column(sa.Text)
    #: 任务租约到期时间。worker 领取时置为 now+lease，崩溃后租约过期即可被重捞。
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 下次可尝试解析的时间，用于失败退避
    next_parse_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    source: Mapped[Source | None] = relationship(back_populates="documents")
    extractions: Mapped[list[Extraction]] = relationship(
        back_populates="raw_document", cascade="all, delete-orphan"
    )
    resources: Mapped[list[Resource]] = relationship(back_populates="raw_document")

    __table_args__ = (
        # worker 领取待解析文档的主查询路径
        sa.Index("ix_raw_document_parse_queue", "parse_status", "next_parse_at"),
        sa.Index("ix_raw_document_source", "source_type", "source_name", "published_at"),
        # 按采集源回溯其产出，以及排查"某条消息到底采没采到"
        sa.Index("ix_raw_document_source_msg", "source_id", "source_msg_id"),
    )
