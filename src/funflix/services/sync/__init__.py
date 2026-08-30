"""本地库与远端库同步：`pull`（remote → local）/ `push`（local → remote）。

破坏性/批量重写操作（`db reset`、`db retag`、`source remove`、
`DELETE /sources/{id}`）不在同步范围内——必须直接对远端库操作，操作前后按
现有的生产变更安全流程（暂停 pipeline → 直连远端执行 → 确认无误 → 恢复）来
做，见 `docs/DESIGN.md` 里对应章节。
"""

from funflix.services.sync.runner import SyncReport, TableSyncResult, pull, push
from funflix.services.sync.tables import JOB_TABLES, SyncTable, sync_tables

__all__ = [
    "JOB_TABLES",
    "SyncReport",
    "SyncTable",
    "TableSyncResult",
    "pull",
    "push",
    "sync_tables",
]
