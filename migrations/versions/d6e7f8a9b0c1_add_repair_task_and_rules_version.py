"""修复机制：新增 repair_task 表，raw_document 加规则集版本戳

Revision ID: d6e7f8a9b0c1
Revises: b1c2d3e4f5a6
Create Date: 2026-10-05

**纯加法、完全可逆。** 不动任何现有列的类型或约束，所以可以在任何时候上，
不需要先跑数据任务。

## 为什么排在收口迁移 `c5d6e7f8a9b0` **之前**

因为 `db reset` 和那条迁移互为前置，不换顺序就是死锁：

- `reset_pipeline_data` 的清表清单从 ORM 元数据推导（`services/maintenance.py`），
  现在包含 `repair_task`，而 PG 分支是单条 `TRUNCATE ... CASCADE` ——
  表不存在就整条失败。所以 reset 要求本迁移已经上了。
- `c5d6e7f8a9b0` 第一句是「`media.work_id` 还有空值就拒绝」的守卫，
  而全量重建的路线是先 reset 清空 media 再让守卫放行。所以它要求 reset 先跑。

本迁移对 `c5d6e7f8a9b0` 零依赖（`repair_task` 刻意不建 media 外键，
`raw_document` 两条迁移都不碰），挪到前面就把环解开了：

    funflix db upgrade d6e7f8a9b0c1    # 本条，纯加法，不碰 media
    funflix db reset --keep-documents  # 清空 media/work/…，留原文和校验历史
    funflix db upgrade head            # c5d6e7f8a9b0，守卫看到 0 行 media 放行

解析规则会一直加下去，每次加都会留下一批「按旧规则算出来、现在看是错的」
数据。这条迁移给常态化修复搭两样东西：

- `repair_task` —— 修复队列。检测（只读、便宜、每轮跑）和应用（破坏性、
  不可逆、要限额）必须分开调度，所以中间需要一张可审计、可续跑的表。
  详见 `models/repair.py`。
- `raw_document.parse_rules_version` —— 规则集版本戳。深层修复（回到原文
  重解析）靠它做廉价预筛，不然每次规则改动都要重跑 213 万文档。
  现有行留 `NULL`，语义上等同于「版本对不上」，会被第一轮 requeue 收走。

## 部分唯一索引

`uq_repair_task_pending` 只约束 `status='pending'` 的行。扫描每轮都会重新
发现同一批问题行，没有它任务表会无限膨胀；而已处理完的历史任务要留着当
审计记录，所以不能做成全表唯一。

PG 和 SQLite 都支持部分索引，两边给同样的 `where`，单测里一样生效。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from funflix.models.base import UTCDateTime

revision: str = "d6e7f8a9b0c1"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")

_PENDING_ONLY = sa.text("status = 'pending'")


def upgrade() -> None:
    op.create_table(
        "repair_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("symptom", sa.String(length=32), nullable=False),
        # 刻意**不建** media.id 的外键：media 行被别的路径删掉（比如同一轮里
        # 它作为多合一的败者）之后，这条任务要留着当审计记录。
        sa.Column("media_id", sa.Uuid(), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detected_at", UTCDateTime(), nullable=False),
        sa.Column("applied_at", UTCDateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repair_task")),
    )
    with op.batch_alter_table("repair_task", schema=None) as batch_op:
        batch_op.create_index("ix_repair_task_queue", ["status", "kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_repair_task_created_at"), ["created_at"], unique=False)
        batch_op.create_index(
            "uq_repair_task_pending",
            ["kind", "media_id"],
            unique=True,
            postgresql_where=_PENDING_ONLY,
            sqlite_where=_PENDING_ONLY,
        )

    with op.batch_alter_table("raw_document", schema=None) as batch_op:
        batch_op.add_column(sa.Column("parse_rules_version", sa.String(length=32), nullable=True))
        batch_op.create_index(
            "ix_raw_document_rules_version",
            ["parse_status", "parse_rules_version"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("raw_document", schema=None) as batch_op:
        batch_op.drop_index("ix_raw_document_rules_version")
        batch_op.drop_column("parse_rules_version")

    with op.batch_alter_table("repair_task", schema=None) as batch_op:
        batch_op.drop_index("uq_repair_task_pending")
        batch_op.drop_index(batch_op.f("ix_repair_task_created_at"))
        batch_op.drop_index("ix_repair_task_queue")
    op.drop_table("repair_task")
