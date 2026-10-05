"""按「攒够 N 条」或「攒够 T 秒」哪个先到就提交的节流器。

一次采集/处理可能要经手几千上万条记录，逐条提交往返太贵，攒到全部跑完
再提交又会在中途出问题（异常、超时、被 kill）时把已经做完的进度全部
赔进去。这里把这个取舍收敛到一个地方：调用方每处理完一批就报一次数量，
攒够阈值条数或超过时间上限（哪个先到）就自动提交，不用在每个循环里
各自维护计数器和时间戳。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from funflix.models import utcnow


@dataclass(slots=True)
class CommitBatcher:
    """会话提交节流器：攒够 `max_pending` 条或距上次提交超过 `max_interval` 就提交。

    Attributes:
        session: 被托管的数据库会话，提交动作发在它身上。
        max_pending: 条数阈值，累计上报量到了就提交。
        max_interval: 时间阈值，距上次提交超过它就提交，避免低速场景迟迟不落盘。
        pending: 自上次提交以来累计上报的变更条数。
        since: 上次提交的时刻（UTC）。
    """

    session: AsyncSession
    max_pending: int = 100
    max_interval: timedelta = timedelta(minutes=1)
    pending: int = field(default=0, init=False)
    since: datetime = field(default_factory=utcnow, init=False)

    async def mark(self, n: int = 1) -> bool:
        """记一次变更；攒够阈值条数或超过时间上限（哪个先到）就提交。"""
        self.pending += n
        if self.pending and (
            self.pending >= self.max_pending or utcnow() - self.since >= self.max_interval
        ):
            await self.session.commit()
            self.pending = 0
            self.since = utcnow()
            return True
        return False

    async def flush(self) -> bool:
        """收尾：无论攒了多少，都强制提交掉剩余的。"""
        if self.pending:
            await self.session.commit()
            self.pending = 0
            self.since = utcnow()
            return True
        return False
