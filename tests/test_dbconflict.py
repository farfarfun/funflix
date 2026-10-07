"""并发写入冲突的识别与重试语义。

本地测试跑在 SQLite 上，不会真的产生 40001/40P01，所以这里用手搓的
`DBAPIError` 固定住判定规则 —— 这条路只在 Postgres 上触发，出问题只会在
线上出，单测是唯一的护栏。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError

from funflix.base import dbconflict
from funflix.base.dbconflict import (
    WRITE_CONFLICT_ATTEMPTS,
    is_write_conflict,
    retry_on_write_conflict,
)


def _err(sqlstate: str | None) -> DBAPIError:
    orig = Exception("boom")
    if sqlstate is not None:
        orig.sqlstate = sqlstate  # type: ignore[attr-defined]
    return DBAPIError("UPDATE resource ...", {}, orig)


class TestIsWriteConflict:
    @pytest.mark.parametrize("sqlstate", ["40001", "40P01"])
    def test_transient_conflicts(self, sqlstate: str) -> None:
        assert is_write_conflict(_err(sqlstate))

    @pytest.mark.parametrize("sqlstate", ["23505", "23503", "42703", None])
    def test_everything_else(self, sqlstate: str | None) -> None:
        """只认那两个瞬时状态码。约束违反重试一万次也是同样的结果，白等。"""
        assert not is_write_conflict(_err(sqlstate))

    def test_non_database_errors(self) -> None:
        """不是 `DBAPIError` 的一律不算 —— `retry_on_write_conflict` 抓的是
        `Exception`（`op` 自己开 SAVEPOINT 时抛什么都可能），判定得自己把门关严。
        """
        assert not is_write_conflict(ValueError("nope"))


class TestRetryOnWriteConflict:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch) -> None:
        monkeypatch.setattr(dbconflict, "WRITE_CONFLICT_BACKOFF", 0.0)

    @pytest.mark.asyncio
    async def test_returns_the_value_on_first_try(self) -> None:
        assert await retry_on_write_conflict(_ok_after(0), what="x") == 1

    @pytest.mark.asyncio
    async def test_retries_until_it_passes(self) -> None:
        """死锁是瞬时的：Postgres 只牺牲一方，另一方已经提交完了，重试就过。"""
        op = _ok_after(WRITE_CONFLICT_ATTEMPTS - 1)
        assert await retry_on_write_conflict(op, what="x") == WRITE_CONFLICT_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retries_are_bounded(self) -> None:
        """重试不是无限的：对面要是一直占着锁，得报出来而不是卡死整条流水线。"""
        calls = 0

        async def always_conflict() -> None:
            nonlocal calls
            calls += 1
            raise _err("40P01")

        with pytest.raises(DBAPIError):
            await retry_on_write_conflict(always_conflict, what="x")
        assert calls == WRITE_CONFLICT_ATTEMPTS

    @pytest.mark.asyncio
    async def test_other_errors_pass_straight_through(self) -> None:
        calls = 0

        async def broken() -> None:
            nonlocal calls
            calls += 1
            raise _err("23505")

        with pytest.raises(DBAPIError):
            await retry_on_write_conflict(broken, what="x")
        assert calls == 1

    @pytest.mark.asyncio
    async def test_attempts_can_be_tightened(self) -> None:
        calls = 0

        async def always_conflict() -> None:
            nonlocal calls
            calls += 1
            raise _err("40001")

        with pytest.raises(DBAPIError):
            await retry_on_write_conflict(always_conflict, what="x", attempts=1)
        assert calls == 1, "attempts=1 就是不重试"


def _ok_after(failures: int):
    """造一个「前 `failures` 次撞车、之后成功」的 op，返回它被调用的次数。"""
    calls = 0

    async def op() -> int:
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise _err("40001")
        return calls

    return op
