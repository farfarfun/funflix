"""Declarative Base、跨方言类型别名与公共 mixin。

设计约束：schema 只使用 SQLite / PostgreSQL 都支持的构造，
方言差异全部收敛到本文件的 `with_variant` 与 `UTCDateTime` 中。
"""

from __future__ import annotations

import os
import time
import uuid
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
        """写库前把值统一转成 UTC。

        Args:
            value: 待写入的 datetime，必须是 tz-aware 的。
            dialect: SQLAlchemy 方言对象，这里用不到。

        Returns:
            转成 UTC 的 datetime；`value` 为 None 时原样返回 None。

        Raises:
            ValueError: 传入 naive datetime。宁可当场报错，也不猜它是哪个时区——
                猜错会让时间悄悄偏移几小时且永远查不出来。
        """
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                f"拒绝写入 naive datetime: {value!r}，请使用 utcnow() 或带 tzinfo 的值"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        """读库后补齐 tzinfo，保证无论哪个数据库取出来都是 UTC-aware 的。

        Args:
            value: 从数据库取出的 datetime，SQLite 上是 naive 的。
            dialect: SQLAlchemy 方言对象，这里用不到。

        Returns:
            UTC-aware 的 datetime；`value` 为 None 时原样返回 None。
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def uuid7() -> uuid.UUID:
    """客户端生成的时间排序主键（RFC 9562 UUIDv7）：48 位毫秒时间戳前缀 + 74 位
    随机数。字典序等于生成顺序，全局唯一且多机并发生成不会冲突——这是本地库
    拉取/推送同步方案的前提（自增整数主键在多机各自写入时必然撞号）。

    标准库要到 3.14 才有 `uuid.uuid7()`，项目下限是 `>=3.12`，这里自己实现。
    时间前缀是为了保留现有代码依赖的"id 与入库先后同序"语义——
    `api/v1/resources.py` 等列表接口靠 `order_by(id.desc())` 做"最新优先"排序，
    换成纯随机的 UUIDv4 会打乱这个顺序。
    """
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & 0x3FFFFFFFFFFFFFFF
    value = ((ts_ms & 0xFFFFFFFFFFFF) << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


#: 主键。PostgreSQL 上是原生 uuid 列，SQLite 上退化成 CHAR(32) 存十六进制
#: （`sa.Uuid` 是 SQLAlchemy 2.0 内置的跨方言类型）。客户端生成（见 `uuid7`），
#: 不依赖数据库分配——多机各自写入互不冲突，也省掉 INSERT...RETURNING 往返。
PkType = sa.Uuid(as_uuid=True)

#: JSON 列。PostgreSQL 上自动升级为 JSONB（可索引、可查询）。
JsonType = sa.JSON().with_variant(JSONB(), "postgresql")

#: 大整数（文件大小等），非主键，两库都用 BIGINT。
BigIntType = sa.BigInteger()


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    统一挂上带命名约定的 `MetaData`：索引、唯一键、外键的名字由约定生成而不是
    数据库自动分配，否则 alembic 在 SQLite 与 PostgreSQL 上会生成不同的约束名，
    迁移脚本没法通用。
    """

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """调试用的简短表示：`<类名 id=主键>`，不展开任何字段值。"""
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
