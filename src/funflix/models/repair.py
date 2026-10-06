"""修复队列 —— 「这一行 media 按现在的规则算是错的，该怎么改」。

## 为什么需要一张表

解析规则永远加不完。每次给 `services/text/normalize.py` 补一个字段名或一条
词表，库里就多出一批「按旧规则算出来、现在看是错的」数据 —— 比如 `作者` 这个
字段名加进 `_SCRAPE_CUT_RE` 之前解析的 4,399 行，作者名整条粘在片名里。
全量重解析 213 万文档不可能每次规则改动都来一遍，所以要有一条常态化的修复路径。

检测和应用**必须分开**，这是这张表存在的全部理由：

- 检测是只读的、便宜的（一次 media 扫描），规则没变就一个任务都不建，
  可以挂在流水线上每轮跑。
- 应用是破坏性的、不可逆的。**两部剧并成一部之后没有任何信息能把它们分回去。**
  所以它需要限额、需要能先 dry-run 核对、需要能中断续跑。

把两件事塞进同一次扫描就只剩一个选择：要么不敢天天跑，要么天天盲改生产库。

## 三类操作

用户的原话是「可能涉及到修改,多合一,删除(多合一冗余的需要删除)」，对应
`RepairKind` 的三个值。**多合一不是独立的一类** —— 它是移动的结果：
一行 media 要搬到的 `(work_id, season)` 已经被别人占着，两行就得并成一行、
败者删掉。这件事 `services/canon/assign.py::assign_identities` 已经做了
（移动 + 撞车合并 + swap 冲突停车三件事在一个步骤里），所以这里只记
`payload["expect_merge"]`，让 dry-run 能提前报出「这轮会发生多少次多合一」。

## 幂等

`uq_repair_task_pending` 是个**部分唯一索引**（只约束 `status='pending'`）。
扫描每轮都会重新发现同一批问题行，没有它任务表会无限膨胀。

反过来，一行 media 在任务 `applied` 之后再次被检出是**预期行为**，不是 bug ——
那说明修完还有残留问题（比如改完标题之后归一键又变了），该再修一轮。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from funflix.models.base import Base, JsonType, PkType, TimestampMixin, UTCDateTime, uuid7


class RepairKind:
    """`RepairTask.kind` 的取值 —— 要对这一行 media 做什么。

    和 `CanonState` 一样用字符串而不是 `enum_col`：这是修复服务内部的调度
    状态，不进 API 响应。
    """

    #: 修改：标题 / 类型 / 年份变了，身份不变。纯 UPDATE，最安全。
    RETITLE = "retitle"
    #: 移动：归一键或季号变了，要搬到另一个 Work /另一季。
    #: 目标已被占用时就地合并（见模块说明的「多合一」）。
    REHOME = "rehome"
    #: 删除：洗完判为垃圾，或者是一行没有任何资源的空壳。
    DELETE = "delete"


class RepairSymptom:
    """为什么这一行被挑出来。只用于审计和报表，不影响执行。

    留这一列是为了能回答「这一轮的 12 万个任务是哪条规则改动引发的」——
    没有它，一次误改规则造成的大面积改写只能靠时间戳去猜。
    """

    #: `series_norm_key(title)` 和所属 Work 的 `norm_key` 不一致了。
    KEY_DRIFT = "key_drift"
    #: `clean_title` / 类型判定的结果变了。
    TITLE_DRIFT = "title_drift"
    #: `looks_like_junk_title` 现在认出它不是作品。
    JUNK = "junk"
    #: 一条资源都没有的空壳 media。
    EMPTY_SHELL = "empty_shell"


class RepairState:
    """`RepairTask.status` 的取值。`pending` 的行就是待办队列。"""

    PENDING = "pending"
    APPLIED = "applied"
    #: 应用时发现已经不需要改了（上一轮顺带修掉了，或被 parse 覆盖了）。
    SKIPPED = "skipped"
    FAILED = "failed"


class RepairTask(TimestampMixin, Base):
    """一行 media 的一个待修复项。"""

    __tablename__ = "repair_task"

    id: Mapped[uuid.UUID] = mapped_column(PkType, primary_key=True, default=uuid7)

    #: 见 `RepairKind`。
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: 见 `RepairSymptom`。
    symptom: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    #: 要修的那一行。**没有外键 `ondelete=CASCADE`**：media 行被别的路径删掉
    #: （比如同一轮里它作为多合一的败者）之后，这条任务要留着当审计记录，
    #: 应用时查不到 media 就记 `skipped`。
    media_id: Mapped[uuid.UUID] = mapped_column(PkType, nullable=False)

    #: 新值。`retitle` 放 title / media_type / year；`rehome` 放
    #: work_norm_key / work_title / season / expect_merge；`delete` 为空。
    #: 用 JSON 而不是摊成列：三类操作的字段集不重叠，摊开会是一张到处是
    #: NULL 的宽表，而这些值只被 `services/repair/apply.py` 读一次。
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)

    #: 见 `RepairState`。
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default=RepairState.PENDING)
    detected_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    applied_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        # 排空队列的主查询路径：按 kind 分批捞 pending。
        # 顺序是 delete → retitle → rehome（理由见 services/repair/apply.py）。
        sa.Index("ix_repair_task_queue", "status", "kind"),
        # 幂等闸：同一行 media 同一类操作最多只能有一个未处理的任务。
        # 见模块说明。SQLite 也支持部分索引，所以单测里一样生效。
        sa.Index(
            "uq_repair_task_pending",
            "kind",
            "media_id",
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        ),
    )
