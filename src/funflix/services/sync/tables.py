"""同步范围与水位列的反射驱动分类。

风格与 `services/maintenance.py::data_tables()` 一致：参与同步的表清单和每
张表用哪一列判断"有没有新变化"，都从 `Base.metadata` 反射推导，不手写清单——
手写漏掉一张表（比如当年漏掉 tag/media_tag）不会报错，只会安静地漏同步。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from funflix.models import Base

#: 按优先级挑水位列：updated_at 覆盖"可变状态"表；created_at 覆盖多数只追加表；
#: checked_at 是 link_check 的特例——它连 created_at 都没有（见 models/check.py）。
_WATERMARK_CANDIDATES = ("updated_at", "created_at", "checked_at")


@dataclass(slots=True, frozen=True)
class SyncTable:
    table: sa.Table
    watermark_column: str
    #: 有 updated_at 的表按"可变状态"处理：upsert 冲突时按水位线做
    #: last-write-wins。没有的表按"只追加"处理：冲突直接跳过，不覆盖。
    mutable: bool


def sync_tables() -> list[SyncTable]:
    """全部参与同步的表，按外键依赖顺序排列（父表在前）。"""
    out: list[SyncTable] = []
    for table in Base.metadata.sorted_tables:
        watermark = next((c for c in _WATERMARK_CANDIDATES if c in table.columns), None)
        if watermark is None:
            raise RuntimeError(
                f"表 {table.name} 没有可用的水位列（updated_at/created_at/checked_at），"
                "无法参与同步"
            )
        out.append(
            SyncTable(table=table, watermark_column=watermark, mutable=watermark == "updated_at")
        )
    return out
