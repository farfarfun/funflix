"""登录账号。

只支撑「运维」区的登录态，不是面向读者的用户体系——没有邮箱、找回密码、
角色分级这些字段，需要时再加。
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from funflix.models.base import Base, PkType, TimestampMixin, uuid7


class User(TimestampMixin, Base):
    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: bcrypt 哈希（含盐），不存明文，也不用可逆加密
    password_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    #: 停用而不是删除：保留创建/修改记录，且不会让 session 里存的 user_id 变成
    #: 悬空引用后又被别的新账号复用
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    __table_args__ = (sa.UniqueConstraint("username", name="uq_user_username"),)
