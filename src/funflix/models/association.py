"""作品 ↔ 资源 的多对多关联。

为什么是多对多而不是一对多：

- 一部作品有多个网盘链接（夸克、阿里、百度各一份）—— 这一向是显然的；
- **一个链接也可能装着多部作品** —— 合集/打包分享里一个链接对应几十部剧。
  用 `resource.media_id` 单外键表达不了后者，只能让链接挂到其中一部上，
  其余作品要么丢失、要么被迫复制出重复的 resource 行。

唯一约束 `(media_id, resource_id)` 保证同一对「作品↔链接」只有一条记录，
这是防止关联表膨胀的最后一道闸。
"""

from __future__ import annotations

import sqlalchemy as sa

from funflix.models.base import Base, PkType, UTCDateTime, utcnow

media_resource = sa.Table(
    "media_resource",
    Base.metadata,
    sa.Column(
        "media_id",
        PkType,
        sa.ForeignKey("media.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "resource_id",
        PkType,
        sa.ForeignKey("resource.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("created_at", UTCDateTime, nullable=False, default=utcnow),
    # 反向查询「这个链接里有哪些作品」
    sa.Index("ix_media_resource_resource", "resource_id"),
)
