"""应用配置。所有项均可通过 `FUNFLIX_` 前缀的环境变量或 .env 覆盖。"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: nltsecret 里都没有、环境变量也没给时的兜底。
#: 用本地 SQLite 而不是报错 —— 让"刚 clone 下来就能跑起来"成立。
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./funflix.db"

#: 同步驱动 → 异步驱动的映射。
#: 密钥库里的 db/url 通常是给同步工具（psycopg2 等）用的、被多个项目共享，
#: 与其要求所有人改成 +asyncpg，不如在这里规范化 —— 同一份配置两边都能用。
_ASYNC_DRIVERS = {
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "postgresql+psycopg2": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
    "mysql+pymysql": "mysql+aiomysql",
    "mysql+mysqldb": "mysql+aiomysql",
    "sqlite": "sqlite+aiosqlite",
}


def to_async_url(url: str) -> str:
    """把同步驱动的连接串换成对应的异步驱动。

    已经是异步驱动（+asyncpg / +aiosqlite / +aiomysql / +psycopg）的原样返回，
    未知方言也原样返回 —— 让 SQLAlchemy 去报它自己的错，别在这里猜。
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    replacement = _ASYNC_DRIVERS.get(scheme.lower())
    return f"{replacement}://{rest}" if replacement else url


def resolve_database_url() -> str:
    """从 nltsecret 读数据库地址：`read_secret("funflix", "db", "url")`。

    只在环境变量 `FUNFLIX_DATABASE_URL` 缺失时才会被调用 ——
    pydantic-settings 的取值顺序是「环境变量 > default_factory」，
    这个顺序是刻意保留的：CI 和测试需要能用环境变量强制指向临时库，
    否则跑个测试就连到生产库上了。

    nltsecret 缺失或未配置时回落到本地 SQLite，不抛异常。
    """
    try:
        from nltsecret import read_secret
    except ImportError:
        logger.debug("未安装 nltsecret，使用默认数据库地址")
        return DEFAULT_DATABASE_URL

    try:
        value = read_secret("funflix", "db", "url")
    except Exception as exc:  # 密钥库损坏 / 权限问题，不该让整个应用起不来
        logger.warning("读取 nltsecret 数据库配置失败，回落到默认值：%s", exc)
        return DEFAULT_DATABASE_URL

    if not value:
        logger.debug("nltsecret 中未配置 funflix/db/url，使用默认数据库地址")
        return DEFAULT_DATABASE_URL

    # 只记方言，不记完整 URL —— 它可能带账号密码
    logger.info("数据库地址来自 nltsecret（%s）", value.split("://", 1)[0])
    return value


def _normalize_database_url(value: str) -> str:
    return to_async_url(value)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FUNFLIX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 基础 ---
    app_name: str = "funflix"
    debug: bool = False
    log_level: str = "INFO"

    # --- 数据库 ---
    #: 取值顺序：环境变量 FUNFLIX_DATABASE_URL > nltsecret(funflix/db/url) > 本地 SQLite。
    #: 切 PG 只需把 nltsecret 里的值改成 postgresql+asyncpg://...，schema 无需变更。
    database_url: str = Field(default_factory=resolve_database_url)
    db_echo: bool = False

    # --- API ---
    api_prefix: str = "/api/v1"
    #: 管理类接口（reparse / recheck / stats）的鉴权 key；为空则这些接口关闭。
    admin_api_key: str | None = None

    # --- 摄入 ---
    #: 单次批量提交的最大条数
    ingest_max_batch: int = 200
    #: 单条原始文本的最大长度，超出直接拒绝（避免 LLM 阶段爆 token）
    ingest_max_content_length: int = 100_000

    @field_validator("database_url", mode="after")
    @classmethod
    def _ensure_async_driver(cls, value: str) -> str:
        """无论来自环境变量还是密钥库，都规范化成异步驱动。"""
        return _normalize_database_url(value)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
