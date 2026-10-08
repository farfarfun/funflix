"""方言无关地拿到带 `ON CONFLICT` 的 INSERT。

`on_conflict_do_nothing` / `on_conflict_do_update` 不在通用的 `sa.insert()`
上，只挂在各方言自己的 `Insert` 上，而这个库要同时跑在 Postgres（生产）和
SQLite（测试）上。两个方言的方法名和签名是一样的，差的只有构造函数 ——
所以按 `session.bind.dialect.name` 分一次就够了，不必养两条落库代码路径。

什么时候该用它：**往另一个进程正在插入的键空间里写行**。CI 里几个 job 是
并行的，「先查有没有、没有再插」这个模式在两句之间有窗口，对面插进来就撞
唯一键。`ON CONFLICT DO NOTHING` 把这个判断挪进同一条语句里，窗口就没了 ——
这比 `base/dbconflict.py` 那套退避重试更彻底：重试是在冲突**发生之后**救，
这里是让冲突根本发生不了。

反过来说，纯 ORM 那种「改对象字段、靠 flush 生成语句」的落库用不上它（见
`services/canon/resolver.py::_persist`，那边只能退避重试）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession


def insert_stmt(session: AsyncSession, table: sa.Table) -> postgresql.Insert | sqlite.Insert:
    """给这张表造一条本方言的 `Insert`，带 `on_conflict_*` 方法。"""
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        return postgresql.insert(table)
    if dialect == "sqlite":
        return sqlite.insert(table)
    raise NotImplementedError(f"不支持的方言: {dialect}")
