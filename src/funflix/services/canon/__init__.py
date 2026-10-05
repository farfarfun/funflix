"""归一服务：把 92 万条「一条分享一行」的 media 收敛成「一部剧一行 + 季子层」。

四个阶段，顺序固定，每一步都可中断续跑：

1. `purge` —— 删掉根本不是作品的行（页面文案、提取码、分享 ID、软件包）。
   实测命中 12.4 万行（13.4%）。
2. `rebuild` —— 用升级后的规则重算键，确定性地建 `Work`、回填
   `media.work_id` / `media.season`，撞车的季就地合并。零 token，
   实测 92.9 万 → 44 万。
3. `resolve` —— 规则搞不定的残局交给 LLM，裁决写 `title_canon`。
   只送候选项 ≥2 的块（实测 4,924 个），单候选项的块没有可并的对象。
4. `apply` —— 按 `title_canon` 把 media 重挂到正确的 Work / 季上。
   CLI 上这一步叫 `canon merge`；模块叫 `apply` 是为了跟 `merge.py`
   那个「把几行并成一行」的共享原子操作区分开。

**为什么不是「清库重跑 parse」**：resource 和 raw_document 是真实采集成本
（197 万 / 211 万行），里面的链接是对的，错的只是归一层怎么给它们分组。
原地改 media 的归属能保住这些数据，也保住了 `link_check` 的校验历史。
"""

from __future__ import annotations

from funflix.services.canon.apply import ApplyReport, apply_canon_decisions
from funflix.services.canon.assign import AssignStats, assign_identities
from funflix.services.canon.merge import MergeStats, merge_media_rows
from funflix.services.canon.purge import PurgeReport, delete_media_rows, purge_junk_media
from funflix.services.canon.rebuild import RebuildReport, rebuild_works
from funflix.services.canon.resolver import ResolveReport, resolve_canon

__all__ = [
    "ApplyReport",
    "AssignStats",
    "MergeStats",
    "PurgeReport",
    "RebuildReport",
    "ResolveReport",
    "apply_canon_decisions",
    "assign_identities",
    "delete_media_rows",
    "merge_media_rows",
    "purge_junk_media",
    "rebuild_works",
    "resolve_canon",
]
