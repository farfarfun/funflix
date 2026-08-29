"""应用配置。所有项均可通过 `FUNFLIX_` 前缀的环境变量或 .env 覆盖。"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: funsecret 里都没有、环境变量也没给时的兜底。
#: 用本地 SQLite 而不是报错 —— 让"刚 clone 下来就能跑起来"成立。
#:
#: 固定放在 `~/.cache/farfarfun/funflix/` 下（而不是 CWD 相对路径），
#: 这样不论从哪个目录执行 `funflix`，读写的都是同一份库。
DEFAULT_DATABASE_PATH = Path.home() / ".cache" / "farfarfun" / "funflix" / "funflix.db"
DEFAULT_DATABASE_URL = f"sqlite+aiosqlite:///{DEFAULT_DATABASE_PATH}"

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


def _fallback_to_default() -> str:
    """落到本地 SQLite 前确保目录存在——只在真正要用到这条兜底路径时才建，
    不在模块导入时就动文件系统。"""
    DEFAULT_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DEFAULT_DATABASE_URL


def resolve_database_url() -> str:
    """从 funsecret 读数据库地址：`read_secret("funflix", "db", "url")`。

    只在环境变量 `FUNFLIX_DATABASE_URL` 缺失时才会被调用 ——
    pydantic-settings 的取值顺序是「环境变量 > default_factory」，
    这个顺序是刻意保留的：CI 和测试需要能用环境变量强制指向临时库，
    否则跑个测试就连到生产库上了。

    funsecret 缺失或未配置时回落到本地 SQLite，不抛异常。
    """
    try:
        from funsecret import read_secret
    except ImportError:
        logger.debug("未安装 funsecret，使用默认数据库地址")
        return _fallback_to_default()

    try:
        value = read_secret("funflix", "db", "url")
    except Exception as exc:  # 密钥库损坏 / 权限问题，不该让整个应用起不来
        logger.warning("读取 funsecret 数据库配置失败，回落到默认值：%s", exc)
        return _fallback_to_default()

    if not value:
        logger.debug("funsecret 中未配置 funflix/db/url，使用默认数据库地址")
        return _fallback_to_default()

    # 只记方言，不记完整 URL —— 它可能带账号密码
    logger.info("数据库地址来自 funsecret（%s）", value.split("://", 1)[0])
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
    #: 取值顺序：环境变量 FUNFLIX_DATABASE_URL > funsecret(funflix/db/url) > 本地 SQLite。
    #: 切 PG 只需把 funsecret 里的值改成 postgresql+asyncpg://...，schema 无需变更。
    database_url: str = Field(default_factory=resolve_database_url)
    db_echo: bool = False

    # --- 搜索 ---
    #: pg_trgm 的相似度阈值，低于它的结果基本是噪声。
    #:
    #: 作为**连接参数**下发（见 base/db.py），而不是写进 WHERE 里：
    #: `similarity(a, b) > 0.2` 是函数调用形式，GIN gin_trgm_ops 索引服务不了它，
    #: 只能全表扫描；换成 `a % b` 操作符后阈值就得由这个 GUC 提供。
    #: 实测 5 万行、3 字关键词：63.9ms（全表扫描）→ 0.235ms（位图索引扫描）。
    search_trgm_threshold: float = 0.2

    # --- API ---
    api_prefix: str = "/api/v1"
    #: 管理类接口（reparse / recheck / stats）的鉴权 key；为空则这些接口关闭。
    admin_api_key: str | None = None

    # --- 后台 worker（见 docs/DESIGN.md §5）---
    #: API 进程内是否顺带跑后台 worker。
    #:
    #: 默认**关闭**，与设计文档不同，理由是两个：一是开着的话 `funflix serve`
    #: 会自己开始调 LLM 和探网盘，一条会真实花钱的副作用不该由"起个 API"隐式触发；
    #: 二是 uvicorn 多 worker 部署时每个进程都会起一份，租约虽然能防重复处理，
    #: 但白白多出几倍的空转轮询。生产建议用独立的 `funflix worker` 进程。
    worker_enabled: bool = False
    #: 两轮扫描之间的间隔
    worker_poll_seconds: int = 60
    #: 任务租约时长。必须显著长于单条任务的正常耗时（LLM 调用可能几十秒），
    #: 否则任务还在跑租约就过期了，会被另一个 worker 重复领取，白烧一次 token。
    worker_lease_seconds: int = 300
    #: 每轮采集**每批**领取多少个到点的源；一轮会循环分批领取直到清空，不是硬上限。
    worker_collect_batch: int = 5
    #: 每轮解析**每批**领取多少条待抽取文本；一轮会循环分批领取直到清空，不是硬上限。
    worker_parse_batch: int = 500
    #: 每轮校验**每批**领取多少条待复查资源；一轮会循环分批领取直到清空，不是硬上限。
    worker_verify_batch: int = 500
    #: 攒够多少条处理完的任务再提交一次，而不是逐条提交——优先吞吐，代价是
    #: 中途崩溃会丢这一撮里已完成但未提交的工作（连带白花的 LLM token/探测次数）；
    #: 重新跑一次的成本被认为远低于逐条 commit 的往返延迟。
    worker_write_batch: int = 10
    #: 每个网盘每秒最多几次探测。默认值从 1.0 提到 5.0——打太快可能触发网盘
    #: 风控、被限流的响应误判成链接失效，是用户在看到这条风险后明确接受、
    #: 换取校验吞吐的选择，不是随手调大的数字。
    worker_verify_rate: float = 5.0
    #: 强制使用某个抽取器；留空则按来源类型自动选
    worker_extractor: str | None = None
    #: 心跳进度日志的打印间隔；<= 0 关闭
    worker_progress_seconds: int = 5

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
