"""LLM 抽取结果留档。

存在的意义有两个：
1. **缓存** —— `(raw_document_id, model, prompt_version)` 唯一，同一 prompt 版本不会二次烧 token。
2. **可回放** —— prompt 迭代后能横向对比新旧版本在同一批文本上的表现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from funflix.models.base import Base, JsonType, PkType, TimestampMixin

if TYPE_CHECKING:
    from funflix.models.raw import RawDocument


class Extraction(TimestampMixin, Base):
    __tablename__ = "extraction"

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    raw_document_id: Mapped[int] = mapped_column(
        PkType, sa.ForeignKey("raw_document.id", ondelete="CASCADE"), nullable=False
    )

    model: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: prompt 改动必须升版本，否则会命中旧缓存拿到过时结果
    prompt_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)

    #: LLM 返回的结构化结果原样存放，不做加工
    output: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)

    input_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    output_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    #: 抽取阶段的质量信号，例如被"原文回查"拦掉的幻觉 URL 数量
    stats: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)

    raw_document: Mapped[RawDocument] = relationship(back_populates="extractions")

    __table_args__ = (
        sa.UniqueConstraint(
            "raw_document_id", "model", "prompt_version", name="uq_extraction_doc_model_version"
        ),
    )
