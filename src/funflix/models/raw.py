"""原始文本。整条流水线的入口与溯源根节点。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from funflix.base.enums import ParseStatus, SourceType, enum_col
from funflix.models.base import Base, JsonType, PkType, TimestampMixin, UTCDateTime, uuid7

#: 解析规则集的版本号。**手工 bump** —— 给 `services/text/normalize.py` 的词表、
#: `services/extract/rule.py` / `sheet.py` 的切分逻辑做了会改变产出的修改之后 +1。
#:
#: 它是 `funflix repair requeue` 的廉价预筛：版本号对不上的文档才有必要回到原文
#: 重解析。213 万文档不可能每次规则改动都全量重跑。
#:
#: **刻意不用源码哈希。** 哈希会把改注释、调格式这类无关变更也算成规则变更，
#: 一次就把 213 万文档全部打回队列 —— 那等于废掉预筛本身。
#:
#: 和 `CANON_PROMPT_VERSION`（归一 prompt）、`extract/llm/prompts.py` 的
#: `PROMPT_VERSION`（绑在 `extraction` 表缓存键上）都是独立的东西，不要混用。
#: v1 → v2：`clean_title` 的两条「剥前缀」规则收紧了（单字母列名要求后面紧跟汉字、
#: 数字行号要求后面不是数字），产出会变，所以按上面的约定 bump。
PARSE_RULES_VERSION = "rules-v2"

if TYPE_CHECKING:
    from funflix.models.extraction import Extraction
    from funflix.models.resource import Resource
    from funflix.models.source import Source


class RawDocument(TimestampMixin, Base):
    """一条未经加工的分享文本。

    `content` 永远保持原样 —— 解析逻辑会迭代，原文是唯一能重跑的依据。
    """

    __tablename__ = "raw_document"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)

    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: sha256(规范化后的 content)。入口去重锚点，挡住重复提交带来的 LLM 开销。
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)

    # --- 来源 ---
    #: 采集源。手工提交的文档没有 source，故可空。
    source_id: Mapped[uuid.UUID | None] = mapped_column(
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
    #: 最近一次尝试解析的时间（成功或失败都算）。为空即"从没解析过"，
    #: 领取/排队时用它把这类文档排到已处理过但待重试的文档前面。
    last_parsed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 最近一次成功解析时用的规则集版本，见 `PARSE_RULES_VERSION`。
    #: `NULL` = 规则版本戳这个机制上线之前解析的，等同于"版本对不上"。
    parse_rules_version: Mapped[str | None] = mapped_column(sa.String(32))

    source: Mapped[Source | None] = relationship(back_populates="documents")
    extractions: Mapped[list[Extraction]] = relationship(
        back_populates="raw_document", cascade="all, delete-orphan"
    )
    resources: Mapped[list[Resource]] = relationship(back_populates="raw_document")

    __table_args__ = (
        # worker 领取待解析文档的主查询路径
        sa.Index("ix_raw_document_parse_queue", "parse_status", "next_parse_at"),
        # 批量解析流水线**翻页**的查询路径，和上面那条领取索引不是一回事：
        # `concurrent_runner.py` 的生产者按 `(last_parsed_at NULLS FIRST, id)`
        # 有序 keyset 翻页（见 `runner.py::keyset_after`），而领取索引的第二列是
        # `next_parse_at`，支撑不了这个排序。
        #
        # 少了它 PG 只能并行全表扫再 top-N 排序：213 万行实测**每翻一页 773ms、
        # 读 2.6GB**，而翻页是每批都要做的。单进程时这被 500 条摊薄了还不致命，
        # 多进程分片跑（`--shard`）就会变成几路并发全表扫，把 I/O 打满。
        #
        # **PG 专属。** SQLite 支持 `ORDER BY ... NULLS FIRST`，却不支持建索引时
        # 写它（3.50 实测报 `unsupported use of NULLS FIRST`）。而索引的 NULL
        # 方向必须和查询一致，否则 PG 拿到也得重排、白建。单测库只有几十行，
        # 走不走索引没有区别，所以按方言跳过，而不是反过来改查询语义去迁就 SQLite。
        sa.Index(
            "ix_raw_document_parse_scan",
            "parse_status",
            sa.text("last_parsed_at NULLS FIRST"),
            "id",
        ).ddl_if(dialect="postgresql"),
        sa.Index("ix_raw_document_source", "source_type", "source_name", "published_at"),
        # `funflix repair requeue` 的查询路径：找出已解析完、但规则集版本对不上的文档。
        # 规则刚 bump 完时几乎全表命中、走不走索引都一样；真正需要它的是
        # **规则没变**的那些轮次 —— 得能一眼证明"没有要重刷的"，而不是扫 213 万行。
        sa.Index("ix_raw_document_rules_version", "parse_status", "parse_rules_version"),
        # 按采集源回溯其产出，以及排查"某条消息到底采没采到"
        sa.Index("ix_raw_document_source_msg", "source_id", "source_msg_id"),
    )
