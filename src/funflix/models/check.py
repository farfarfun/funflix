"""链接校验历史。只追加，不更新。完全独立于 `resource` 表，没有外键。

保留时序才能回答"这条链接什么时候挂的""某网盘最近整体失效率是不是异常"——
后者是判断"探针本身挂了"还是"链接真失效了"的关键信号。

不持有 resource_id：resource 表会因为重解析被整表清空重建，但校验历史成本
最高（每条都要真实探测网盘接口），不该跟着陪葬，也不该在数据模型上跟
resource 的生命周期有任何耦合——哪怕是可空外键。身份与查询都锚定在
(provider, share_id)（resource.py 里同样是全局去重锚点，而不是 url 字符串，
同一分享的 URL 写法会变）；`url` 单独存一份当次探测用的快照，供人工核对
或 resource 已不存在时仍能看到原始链接，不代表当前有效地址。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from funflix.base.enums import CheckStatus, Provider, enum_col
from funflix.models.base import Base, PkType, UTCDateTime, utcnow


class LinkCheck(Base):
    __tablename__ = "link_check"

    id: Mapped[int] = mapped_column(PkType, primary_key=True)
    #: 链接身份，见 resource.py 里 (provider, share_id) 是全局去重锚点、而不是 url
    #: 字符串的说明。这两列而不是外键才是这张表跟 resource 之间唯一的关联方式。
    provider: Mapped[Provider] = mapped_column(enum_col(Provider), nullable=False)
    share_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    #: 探测当时 resource.url 的快照，仅供人工核对／resource 行已不存在时溯源，
    #: 不代表"当前"地址——同一分享的 URL 写法可能变化，识别仍然按上面两列。
    url: Mapped[str] = mapped_column(sa.String(2048), nullable=False)

    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    status: Mapped[CheckStatus] = mapped_column(enum_col(CheckStatus), nullable=False)
    http_code: Mapped[int | None] = mapped_column(sa.Integer)
    #: 探针实现标识（如 "quark-anon-v1"），探针换实现后能区分历史数据
    probe: Mapped[str | None] = mapped_column(sa.String(64))
    detail: Mapped[str | None] = mapped_column(sa.Text)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)

    __table_args__ = (
        sa.Index("ix_link_check_identity_time", "provider", "share_id", "checked_at"),
    )
