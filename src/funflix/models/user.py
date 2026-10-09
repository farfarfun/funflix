"""登录账号。

列定义全部来自 `funauth.UserMixin`，这里只负责把它落成本仓 `Base` 上的一张
具体表。角色两级：`GUEST` 能进站看内容，`ADMIN` 额外能进「运维」区。

唯一约束刻意留在本仓而不是 mixin 里 —— 约束名会进迁移、进 `ON CONFLICT`，
已经建好的生产库里那个名字是什么样就得是什么样，不能由 funauth 的版本决定。
"""

from __future__ import annotations

import sqlalchemy as sa
from funauth import UserMixin

from funflix.models.base import Base, TimestampMixin


class User(UserMixin, TimestampMixin, Base):
    """登录账号：用户名 + bcrypt 密码哈希 + 角色 + 启用标记。

    用本仓的 `TimestampMixin` 而不是 funauth 自带的那份：两边 DDL 一致（都是
    `DateTime(timezone=True)`），但本仓所有表的时间列应当来自同一处定义。

    字段语义见 `funauth.UserMixin`；对这张表的全部操作走
    `funflix.services.account.accounts`。
    """

    __tablename__ = "user"

    __table_args__ = (sa.UniqueConstraint("username", name="uq_user_username"),)
