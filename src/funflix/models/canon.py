"""归一裁决缓存 —— LLM 把"这个脏标题属于哪部作品"的判定结果落在这里。

这张表是整件事**不回退、不重复付费**的支点：

- 不回退：`_upsert_media` 落库前先查这里。重跑 parse、新数据入库都会沿用
  已有的裁决，不会把好不容易并好的作品重新拆开。没有它的话，每次重跑
  都要再付一遍 LLM 的钱才能回到同一个状态。
- 不重复付费：按 `norm_key` 命中即复用。同一个脏标题在频道里重复出现
  几十次（每更新一集重发一条），只付第一次。
- 可审计：哪个 key 被并到哪部作品、谁判的、什么时候判的、置信度多少，
  全部留痕。判错了改这一行再重跑 merge 即可，不用动 media 表。
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from funflix.base.enums import MediaType, enum_col
from funflix.models.base import Base, TimestampMixin, UTCDateTime
from funflix.models.media import UNKNOWN_YEAR

#: 归一 prompt 的版本号。改了 prompt 或工具 schema 就要 +1。
#:
#: **和 `extract/llm/prompts.py` 里的 `PROMPT_VERSION` 是两个独立的东西**，
#: 不要混用：那一个绑在 `extraction` 表的缓存键 `(raw_document_id, model,
#: prompt_version)` 上，动它会让两百万条已缓存的抽取结果全部失效重跑。
CANON_PROMPT_VERSION = "canon-v1"


class CanonState:
    """`TitleCanon.status` 的取值。

    用字符串而不是像 `MediaType` 那样走 `enum_col`：这是任务调度状态，
    只在归一服务内部流转，不进 API 响应，没必要为它引一个枚举类型。
    """

    PENDING = "pending"
    DECIDED = "decided"
    REJECTED = "rejected"


class TitleCanon(TimestampMixin, Base):
    """一个脏标题归一键 → 它属于哪部作品的哪一季。"""

    __tablename__ = "title_canon"

    #: **系列身份键，即 `series_norm_key(title)`** —— 不是逐标题的 `norm_key`。
    #: 主键，一个键只有一条裁决。
    #:
    #: 键空间这件事必须和三处写法完全一致，不然裁决会被静默绕过：
    #: `canon/resolver.py` 按它写入，`extract/runner.py` 的防回退查询按它查，
    #: `canon/apply.py::_media_ids_for_keys` 和 `services/repair/plan.py::plan_key`
    #: 按它回头找 media。用逐标题的 `norm_key`（不剥季、不剥外文原名、不收敛重复
    #: token）去查就查不中已裁决的行 —— 花钱得出的结论入库时被绕过，正是这张表
    #: 要防的事。
    norm_key: Mapped[str] = mapped_column(sa.String(500), primary_key=True)

    #: 裁决出的规范作品。`is_junk=True` 时为空。
    work_norm_key: Mapped[str | None] = mapped_column(sa.String(500), index=True)
    work_title: Mapped[str | None] = mapped_column(sa.String(500))
    #: 季号。None = 模型也拿不准，merge 时按 `NO_SEASON` 处理。
    season: Mapped[int | None] = mapped_column(sa.Integer)

    media_type: Mapped[MediaType] = mapped_column(
        enum_col(MediaType), nullable=False, default=MediaType.UNKNOWN
    )
    year: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=UNKNOWN_YEAR)

    #: 这个标题根本不是一部作品（页面文案、提取码、纯数字、分享 ID）。
    #: 规则层的 `looks_like_junk_title` 已经拦掉了明显的，这里兜住需要语义
    #: 判断才看得出来的（`从大主宰开始打卡` 是小说、`描述大主宰导演马建平` 是抓取残渣）。
    is_junk: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    #: 见 `CanonState`。任务可中断续跑就靠这一列：下次只捞 pending 的，
    #: 已经付过钱的裁决不会白花。
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default=CanonState.PENDING)

    confidence: Mapped[float | None] = mapped_column(sa.Float)
    model: Mapped[str | None] = mapped_column(sa.String(128))
    prompt_version: Mapped[str | None] = mapped_column(sa.String(32))
    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (sa.Index("ix_title_canon_status", "status"),)
