"""后台任务执行。

把「状态列 + 租约 + 重试次数」当队列用，让整条流水线在进程崩溃后
仍是 at-least-once，而不是"丢了就没了"。见 docs/DESIGN.md §5。

- `claim`：原子领取与租约
- `tasks`：采集 / 解析 / 校验三类任务的单批执行
- `scheduler`：常驻轮询循环
"""

from __future__ import annotations

from funflix.worker.claim import (
    DEFAULT_LEASE,
    Claimed,
    claim_documents,
    claim_resources,
    claim_sources,
)
from funflix.worker.scheduler import (
    CycleReport,
    StaleSummary,
    Worker,
    progress_heartbeat,
    spawn,
    stale_summary,
)
from funflix.worker.tasks import (
    BatchReport,
    run_collect_batch,
    run_parse_batch,
    run_verify_batch,
)

__all__ = [
    "DEFAULT_LEASE",
    "BatchReport",
    "Claimed",
    "CycleReport",
    "StaleSummary",
    "Worker",
    "claim_documents",
    "claim_resources",
    "claim_sources",
    "progress_heartbeat",
    "run_collect_batch",
    "run_parse_batch",
    "run_verify_batch",
    "spawn",
    "stale_summary",
]
