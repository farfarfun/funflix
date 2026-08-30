"""同步范围与水位列的反射驱动分类。

风格与 `services/maintenance.py::data_tables()` 一致：参与同步的表清单和每
张表用哪一列判断"有没有新变化"，都从 `Base.metadata` 反射推导，不手写清单——
手写漏掉一张表（比如当年漏掉 tag/media_tag）不会报错，只会安静地漏同步。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

import sqlalchemy as sa

from funflix.models import Base

#: 按优先级挑水位列：updated_at 覆盖"可变状态"表；created_at 覆盖多数只追加表；
#: checked_at 是 link_check 的特例——它连 created_at 都没有（见 models/check.py）。
_WATERMARK_CANDIDATES = ("updated_at", "created_at", "checked_at")

#: 每个 pipeline job 实际读写的表——跟 `sync_tables()` 的全表清单不同，这份
#: 映射没法从 metadata 反射出来，是从 collect/parse/verify 的实际查询和写入
#: 路径里读出来的领域知识。job 改动了读写范围但忘记同步这里，后果是该 job
#: 同步不到新涉及的表（静默的功能性缺失，不会报错，也不会同步错数据）——
#: 加表/加字段时留意一下。
JOB_TABLES: dict[str, tuple[str, ...]] = {
    "collect": ("source", "raw_document"),
    "parse": (
        "raw_document",
        "extraction",
        "media",
        "resource",
        "tag",
        "media_resource",
        "media_tag",
    ),
    "verify": ("resource", "link_check", "media", "media_resource"),
}


@dataclass(slots=True, frozen=True)
class SyncTable:
    table: sa.Table
    watermark_column: str
    #: 有 updated_at 的表按"可变状态"处理：upsert 冲突时按水位线做
    #: last-write-wins。没有的表按"只追加"处理：冲突直接跳过，不覆盖。
    mutable: bool


def sync_tables(names: Collection[str] | None = None) -> list[SyncTable]:
    """参与同步的表，按外键依赖顺序排列（父表在前）。

    `names` 为空时返回全部表；给定时只返回这些表（仍按依赖顺序），且校验
    每个名字都能对上一张真实的表——传错名字（比如 job 改名但 JOB_TABLES
    没跟着改）会在这里直接报错，而不是安静地漏同步。
    """
    out: list[SyncTable] = []
    for table in Base.metadata.sorted_tables:
        if names is not None and table.name not in names:
            continue
        watermark = next((c for c in _WATERMARK_CANDIDATES if c in table.columns), None)
        if watermark is None:
            raise RuntimeError(
                f"表 {table.name} 没有可用的水位列（updated_at/created_at/checked_at），"
                "无法参与同步"
            )
        out.append(
            SyncTable(table=table, watermark_column=watermark, mutable=watermark == "updated_at")
        )
    if names is not None:
        missing = set(names) - {spec.table.name for spec in out}
        if missing:
            raise ValueError(f"sync_tables: 不存在的表名 {sorted(missing)}")
    return out
