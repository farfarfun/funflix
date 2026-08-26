"""Declarative Base、跨方言类型别名与公共 mixin。

设计约束：schema 只使用 SQLite / PostgreSQL 都支持的构造，
方言差异全部收敛到本文件的 `with_variant` 与 `UTCDateTime` 中。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 约束统一命名。SQLite 不支持 ALTER 约束，Alembic 只能用 batch 模式重建表，
# 而重建表要求约束有确定的名字 —— 缺了这个命名约定，后续迁移会直接失败。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """当前 UTC 时间（tz-aware）。全库时间统一走这里。"""
    return datetime.now(UTC)


class UTCDateTime(sa.types.TypeDecorator):
    """始终以 UTC-aware datetime 进出的 DateTime。

    SQLite 没有原生时间类型，`DateTime(timezone=True)` 读回来是 naive 的，
    而 PostgreSQL 读回来是 aware 的 —— 不处理的话同一份代码在两个库上行为不一致。
    这里在绑定期强制转 UTC，在返回期补齐 tzinfo，抹平差异。
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                f"拒绝写入 naive datetime: {value!r}，请使用 utcnow() 或带 tzinfo 的值"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


#: 自增主键。SQLite 只有 INTEGER PRIMARY KEY 才是 rowid 别名并自增，BIGINT 不行。
PkType = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

#: JSON 列。PostgreSQL 上自动升级为 JSONB（可索引、可查询）。
JsonType = sa.JSON().with_variant(JSONB(), "postgresql")

#: 大整数（文件大小等），非主键，两库都用 BIGINT。
BigIntType = sa.BigInteger()


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class TimestampMixin:
    """创建 / 更新时间。默认值在 Python 侧生成，避免依赖数据库函数。"""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
