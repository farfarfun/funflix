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
#: 路径里读出来的领域知识。这里只需要写 job 自己关心的表，不用手动补外键
#: 指向的祖先表（比如 parse 不用写 "source"）——`sync_tables()` 会自动算出
#: 外键依赖闭包补全。job 改动了读写范围但忘记同步这里，后果是该 job 同步不到
#: 新涉及的表（静默的功能性缺失，不会报错，也不会同步错数据）——加表/加字段
#: 时留意一下。
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


def _fk_closure(names: set[str]) -> set[str]:
    """把 `names` 扩展成外键依赖闭包。

    `JOB_TABLES` 只记录一个 job 自己读写的表，不代表这些表之间没有外键指向
    闭包之外的表（比如 parse 读写 `raw_document`，但 `raw_document.source_id`
    外键指向 `source`）。如果只同步 `names` 字面写的那几张，本地库开着
    `foreign_keys=ON` 时插入会报 `IntegrityError`，还会被 `_apply_rows` 的
    冲突降级逻辑误判成"业务唯一键冲突"——这正是外键关系本身没被同步覆盖到，
    不是真的数据冲突。用闭包而不是让 `JOB_TABLES` 手写全部祖先表：手写会在
    加一层新外键时又漏掉，闭包从 `Base.metadata` 现算，不会跟着腐化。
    """
    closure = set(names)
    frontier = set(names)
    while frontier:
        next_frontier: set[str] = set()
        for table_name in frontier:
            for fk in Base.metadata.tables[table_name].foreign_keys:
                target = fk.column.table.name
                if target not in closure:
                    closure.add(target)
                    next_frontier.add(target)
        frontier = next_frontier
    return closure


def sync_tables(names: Collection[str] | None = None) -> list[SyncTable]:
    """参与同步的表，按外键依赖顺序排列（父表在前）。

    `names` 为空时返回全部表；给定时返回这些表加上它们的外键依赖闭包（见
    `_fk_closure`），仍按依赖顺序。传错名字（比如 job 改名但 JOB_TABLES 没
    跟着改）会在这里直接报错，而不是安静地漏同步。
    """
    all_names = {t.name for t in Base.metadata.sorted_tables}
    selected: set[str] | None = None
    if names is not None:
        missing = set(names) - all_names
        if missing:
            raise ValueError(f"sync_tables: 不存在的表名 {sorted(missing)}")
        selected = _fk_closure(set(names))

    out: list[SyncTable] = []
    for table in Base.metadata.sorted_tables:
        if selected is not None and table.name not in selected:
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
    return out
