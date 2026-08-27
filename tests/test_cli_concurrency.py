"""`_process_concurrently`：`parse`/`verify` 共用的有界并发 helper。

不走真实数据库——`session_scope` 被替换成一个记账用的假会话，
这里只关心并发调度本身的两条保证：一个任务的异常不连累其余任务，
以及并发度确实被信号量卡住，不会超过 `concurrency`。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from funflix import cli
from funflix.base import db as db_module


class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
async def test_process_concurrently_isolates_a_failing_task(monkeypatch) -> None:
    sessions: list[FakeSession] = []

    @asynccontextmanager
    async def fake_session_scope():
        session = FakeSession()
        sessions.append(session)
        yield session

    monkeypatch.setattr(db_module, "session_scope", fake_session_scope)

    async def handle(_session: FakeSession, item: str) -> str:
        if item == "boom":
            raise RuntimeError("kaboom")
        return item.upper()

    results = await cli._process_concurrently(["a", "boom", "b"], handle, concurrency=2)

    assert results == ["A", results[1], "B"]
    assert isinstance(results[1], RuntimeError)
    assert sum(s.committed for s in sessions) == 2, "失败的那条不该被提交"
    assert sum(s.rolled_back for s in sessions) == 1, "只有失败的那条需要回滚"


@pytest.mark.asyncio
async def test_process_concurrently_respects_the_concurrency_limit(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_session_scope():
        yield FakeSession()

    monkeypatch.setattr(db_module, "session_scope", fake_session_scope)

    active = 0
    peak = 0

    async def handle(_session: FakeSession, _item: int) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    await cli._process_concurrently(list(range(10)), handle, concurrency=3)

    assert peak <= 3
