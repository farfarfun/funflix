"""持续修复：让「解析规则一直在加」变成可运维的常态。

规则永远加不完，每次加都会留下一批「按旧规则算出来、现在看是错的」数据。
所以这里不是一次性迁移脚本，而是三条可以反复跑的命令：

| 模块 | 命令 | 性质 |
|---|---|---|
| `scan.py` | `funflix repair scan` | 只读 + 只写任务表。规则没变就零写入，可每轮跑 |
| `apply.py` | `funflix repair apply` | 破坏性、不可逆、带限额和爆炸半径闸门 |
| `requeue.py` | `funflix repair requeue` | 把规则版本过期的文档打回 parse 队列 |

`plan.py` 是 `scan` 的纯函数判定层，复用 `canon/lookup.py::resolve_target`
—— 修复的终点必须和 parse 现在会产出的结果一字不差，否则两条路互相拆台。

浅层（scan + apply）从已清洗的 `media.title` 重算，深层（requeue）回到
`raw_document.content` 重解析。两层各自独立调度，互不等待。
"""

from funflix.services.repair.apply import (
    ApplyReport,
    BlastRadiusExceeded,
    apply_repairs,
)
from funflix.services.repair.plan import MediaFacts, RepairPlan, plan_key, plan_repair
from funflix.services.repair.requeue import RequeueReport, requeue_stale_documents
from funflix.services.repair.scan import ScanReport, scan_media

__all__ = [
    "ApplyReport",
    "BlastRadiusExceeded",
    "MediaFacts",
    "RepairPlan",
    "RequeueReport",
    "ScanReport",
    "apply_repairs",
    "plan_key",
    "plan_repair",
    "requeue_stale_documents",
    "scan_media",
]
