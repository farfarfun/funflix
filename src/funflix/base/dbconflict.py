"""并发写入冲突的识别与重试 —— 多个节点同时写同一批行时的通用兜底。

CI 里几个 job 是**并行**的：parse 四个分片在重建 `resource`、verify 在把探测
结论写回同样的行、`db relink-checks` 在按批回填历史结论。它们的加锁顺序互不
相同，于是 Postgres 时不时判出死锁、牺牲掉其中一方。

这类错误的关键性质是**瞬时**：冲突的另一方已经提交完了，重试一次基本就过。
所以不该让它冒到命令层把整步判失败 —— 实测代价是整个 verify job 在
`Relink` 这一步退出 1，`funflix verify` 根本没跑到。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from farlog import getLogger
from sqlalchemy.exc import DBAPIError

logger = getLogger("funflix")

#: 一个操作总共尝试几次（含第一次）。
WRITE_CONFLICT_ATTEMPTS = 3

#: 退避基数（秒），第 n 次重试前睡 `n * 基数`。递增是为了别抢着跟对面再撞一次。
WRITE_CONFLICT_BACKOFF = 0.5

#: Postgres 的并发写入冲突 SQLSTATE：40001 序列化失败、40P01 检测到死锁。
_WRITE_CONFLICT_SQLSTATES = frozenset({"40001", "40P01"})


def is_write_conflict(err: BaseException) -> bool:
    """判断一个异常是不是「重试就能过」的并发写入冲突。

    看 SQLSTATE 而不是 `isinstance` 具体的驱动异常类：asyncpg 的
    `DeadlockDetectedError` 会被 SQLAlchemy 包成通用的 `DBAPIError`，具体
    子类反而抓不到。SQLite 没有这些状态码，所以本地测试走不到重试分支 ——
    这条路只在 Postgres 上生效。
    """
    return isinstance(err, DBAPIError) and (
        getattr(err.orig, "sqlstate", None) in _WRITE_CONFLICT_SQLSTATES
    )


async def retry_on_write_conflict[T](
    op: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = WRITE_CONFLICT_ATTEMPTS,
) -> T:
    """跑 `op`，撞上并发写入冲突时退避重试，别的异常原样抛出。

    `op` 必须是**可重跑的**：死锁会让 Postgres 把当前事务打进 aborted 状态，
    调用方得自己保证重跑前事务回到可用状态 —— 要么 `op` 自己开
    `session.begin_nested()`（回滚到 SAVEPOINT 就能恢复，同
    `extract/runner.py` 里对 `IntegrityError` 的处理），要么用独立会话。
    只重跑 `session.execute(...)` 是不行的，第二次会直接报
    「current transaction is aborted」。

    Args:
        op: 要跑的协程工厂。每次尝试都重新调用它。
        what: 日志里指代这个操作的说法，例如「这批 20 条校验结论」。
        attempts: 总尝试次数（含第一次）。

    Raises:
        Exception: `op` 抛出的任何非冲突异常；以及冲突重试耗尽后的最后一次。
    """
    for attempt in range(attempts):
        try:
            return await op()
        except Exception as err:
            if not is_write_conflict(err) or attempt == attempts - 1:
                raise
            delay = WRITE_CONFLICT_BACKOFF * (attempt + 1)
            logger.warning(f"{what} 落库撞车（第 {attempt + 1} 次），{delay:.1f}s 后重试")
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
