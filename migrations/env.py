"""Alembic 运行时环境（异步引擎）。"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from funflix.base.config import get_settings

# 必须导入 models 包，才能让所有表注册到 Base.metadata 上供 autogenerate 使用
from funflix.models import Base
from funflix.models.base import UTCDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """自定义类型渲染。

    Alembic 默认渲染对这两种类型都会产出「引用了却没 import」的代码，
    生成的迁移文件一运行就 NameError：
    - JSON 的 PG 变体渲染成 `postgresql.JSONB(astext_type=Text())`，缺 Text 的 import；
    - 自定义 TypeDecorator 渲染成 `funflix.models.base.UTCDateTime(...)`，缺包的 import。
    这里显式给出渲染形式并登记对应 import。
    """
    if type_ != "type":
        return False
    imports = autogen_context.imports  # type: ignore[attr-defined]
    if isinstance(obj, UTCDateTime):
        imports.add("from funflix.models.base import UTCDateTime")
        return "UTCDateTime()"
    if isinstance(obj, sa.JSON):
        imports.add("from sqlalchemy.dialects import postgresql")
        return "sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')"
    return False


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        render_item=render_item,
        # SQLite 不支持 ALTER 约束/列，batch 模式会重建表来实现变更。
        # 这依赖 models/base.py 里的 NAMING_CONVENTION 给约束确定的名字。
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
