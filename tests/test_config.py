from __future__ import annotations

import sys
from types import ModuleType

import pytest

from funflix.base import config as config_module
from funflix.base.config import (
    DEFAULT_DATABASE_URL,
    Settings,
    resolve_database_url,
    to_async_url,
)


class TestToAsyncUrl:
    """密钥库里的 db/url 通常是给同步工具用的、被多个项目共享，
    所以规范化放在代码里，而不是要求改密钥。"""

    @pytest.mark.parametrize(
        ("sync_url", "async_url"),
        [
            ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
            ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
            ("postgresql+psycopg2://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
            ("mysql://u:p@h/db", "mysql+aiomysql://u:p@h/db"),
            ("mysql+pymysql://u:p@h/db", "mysql+aiomysql://u:p@h/db"),
            ("sqlite:///./x.db", "sqlite+aiosqlite:///./x.db"),
        ],
    )
    def test_upgrades_sync_drivers(self, sync_url: str, async_url: str) -> None:
        assert to_async_url(sync_url) == async_url

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+asyncpg://u:p@h/db",
            "sqlite+aiosqlite:///./x.db",
            "mysql+aiomysql://u:p@h/db",
        ],
    )
    def test_leaves_async_drivers_untouched(self, url: str) -> None:
        assert to_async_url(url) == url

    def test_is_idempotent(self) -> None:
        assert to_async_url(to_async_url("postgresql://u:p@h/db")) == (
            "postgresql+asyncpg://u:p@h/db"
        )

    def test_unknown_dialect_passes_through(self) -> None:
        """未知方言原样放行，让 SQLAlchemy 报它自己的错，别在这里猜。"""
        assert to_async_url("oracle://u:p@h/db") == "oracle://u:p@h/db"

    def test_malformed_url_passes_through(self) -> None:
        assert to_async_url("不是一个url") == "不是一个url"

    def test_preserves_query_params(self) -> None:
        assert to_async_url("postgresql://u:p@h/db?sslmode=require") == (
            "postgresql+asyncpg://u:p@h/db?sslmode=require"
        )


@pytest.fixture
def fake_nltsecret(monkeypatch):
    """装一个假的 nltsecret 模块，避免测试读到真实密钥库。"""

    def install(value, *, raises: Exception | None = None):
        module = ModuleType("nltsecret")

        def read_secret(cate1, cate2, cate3="", *args, **kwargs):
            if raises:
                raise raises
            assert (cate1, cate2, cate3) == ("funflix", "db", "url")
            return value

        module.read_secret = read_secret  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "nltsecret", module)

    return install


class TestResolveDatabaseUrl:
    def test_uses_value_from_nltsecret(self, fake_nltsecret) -> None:
        fake_nltsecret("postgresql+asyncpg://u:p@host/db")
        assert resolve_database_url() == "postgresql+asyncpg://u:p@host/db"

    def test_falls_back_when_not_configured(self, fake_nltsecret) -> None:
        fake_nltsecret(None)
        assert resolve_database_url() == DEFAULT_DATABASE_URL

    def test_falls_back_when_value_is_empty(self, fake_nltsecret) -> None:
        fake_nltsecret("")
        assert resolve_database_url() == DEFAULT_DATABASE_URL

    def test_falls_back_when_nltsecret_missing(self, monkeypatch) -> None:
        """没装 nltsecret 也要能跑起来 —— 否则"clone 下来直接跑"就不成立。"""
        monkeypatch.setitem(sys.modules, "nltsecret", None)
        assert resolve_database_url() == DEFAULT_DATABASE_URL

    def test_falls_back_when_read_secret_raises(self, fake_nltsecret) -> None:
        """密钥库损坏不该让整个应用起不来。"""
        fake_nltsecret(None, raises=RuntimeError("密钥库损坏"))
        assert resolve_database_url() == DEFAULT_DATABASE_URL

    def test_does_not_log_full_url(self, fake_nltsecret, caplog) -> None:
        """URL 可能带账号密码，日志里只该出现方言。"""
        fake_nltsecret("postgresql+asyncpg://user:secret-password@host/db")
        with caplog.at_level("INFO", logger=config_module.__name__):
            resolve_database_url()
        assert "secret-password" not in caplog.text
        assert "postgresql+asyncpg" in caplog.text


class TestSettingsPrecedence:
    def test_env_var_wins_over_nltsecret(self, monkeypatch, fake_nltsecret) -> None:
        """这个顺序是安全底线：测试/CI 必须能强制指向临时库，
        否则跑一次测试就连到生产库上了。"""
        fake_nltsecret("postgresql+asyncpg://u:p@prod/db")
        monkeypatch.setenv("FUNFLIX_DATABASE_URL", "sqlite+aiosqlite:///./test.db")

        assert Settings().database_url == "sqlite+aiosqlite:///./test.db"

    def test_nltsecret_used_when_env_absent(self, monkeypatch, fake_nltsecret) -> None:
        fake_nltsecret("postgresql+asyncpg://u:p@host/db")
        monkeypatch.delenv("FUNFLIX_DATABASE_URL", raising=False)

        assert Settings().database_url == "postgresql+asyncpg://u:p@host/db"

    def test_sync_url_from_nltsecret_is_upgraded(self, monkeypatch, fake_nltsecret) -> None:
        """密钥库里存的是同步驱动，直接拿来建异步引擎会在运行期才炸。"""
        fake_nltsecret("postgresql://u:p@host/db")
        monkeypatch.delenv("FUNFLIX_DATABASE_URL", raising=False)

        assert Settings().database_url == "postgresql+asyncpg://u:p@host/db"

    def test_sync_url_from_env_is_also_upgraded(self, monkeypatch, fake_nltsecret) -> None:
        fake_nltsecret(None)
        monkeypatch.setenv("FUNFLIX_DATABASE_URL", "sqlite:///./x.db")

        assert Settings().database_url == "sqlite+aiosqlite:///./x.db"

    def test_is_sqlite_reflects_resolved_url(self, monkeypatch, fake_nltsecret) -> None:
        fake_nltsecret("postgresql+asyncpg://u:p@host/db")
        monkeypatch.delenv("FUNFLIX_DATABASE_URL", raising=False)
        assert Settings().is_sqlite is False

        monkeypatch.setenv("FUNFLIX_DATABASE_URL", "sqlite+aiosqlite:///./x.db")
        assert Settings().is_sqlite is True
