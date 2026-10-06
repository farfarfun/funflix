"""归一迁移 B：media 身份收口为 (work_id, season)

Revision ID: c5d6e7f8a9b0
Revises: d6e7f8a9b0c1
Create Date: 2026-10-05

迁移 A 是纯加法，这一条才是**收口**：

- `media.work_id` → NOT NULL（一季必须属于某部作品）
- `media.season` → NOT NULL，默认 0（`NO_SEASON`）
- 加 `uq_media_season(work_id, season)`
- 删 `uq_media_identity(norm_key, media_type, year)`

## 为什么必须先跑 `canon rebuild`

`work_id` 设 NOT NULL 之前，每一行都得有归属。历史 92 万行是迁移 A 之后
才有这一列的，全是 NULL。所以这条迁移**自带一道闸**：发现还有 NULL 就
直接报错退出，而不是让 PG 在扫到第一行时抛一条看不懂的
`column "work_id" contains null values`。

实际采用的顺序是**全量重建**，而不是原地迁移历史数据 —— 生产库里
`media.work_id` 非空的只有 80 行（894,162 行里），`title_canon` 80 行全是
pending，work 层等于不存在，没有「历史数据」值得原地搬：

    funflix db upgrade d6e7f8a9b0c1    # 纯加法，不碰 media
    funflix db reset --keep-documents  # 清空 media，留 raw_document/link_check/source
    funflix db upgrade head            # 这一条。守卫看到 0 行 media 才放行
    funflix parse --limit 20000        # 重解析，天生带 work_id
    funflix db relink-checks           # 把 84 万条链接的校验结论接回来
    funflix canon resolve --apply      # 要花钱
    funflix canon merge   --apply

原地迁移的路线（`canon purge` → `rebuild` → `merge` 之后再上这一条）在结构上
仍然成立，守卫就是为它准备的；只是本项目没走。

## 为什么两个约束不能并存，也不能分两次迁移

`media.norm_key` 从「这一条分享自己的脏键」改成「所属作品的键」之后，一部剧
的所有季共享同一个 norm_key。`uq_media_identity` 里 `media_type` / `year`
对同一部剧的各季往往也相同，于是第二季插进去就撞唯一键 ——
**旧约束必须和新身份同时切换**，中间不存在两者都满足的状态。
这也是 `services/extract/runner.py` 的改造（查 `title_canon` 落
`(work_id, season)`）和这条迁移属于同一次变更的原因。

## 可逆性

`downgrade()` 能把结构还原，但**还原不了语义**：`norm_key` 已经被写成作品键，
降级回去之后 `uq_media_identity` 很可能挂着重复值，所以这里同样先查一遍、
有重复就报错退出，提示先自行处理。宁可降级失败，也不要静默删行去凑约束。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `models/media.py` 的 `NO_SEASON`。这里写字面量而不是 import ——
#: 迁移脚本要能在模型继续演进之后仍然复现当时的 DDL。
NO_SEASON = 0


def _guard_no_unassigned_media() -> None:
    """还有 media 没归属就别往下走。"""
    bind = op.get_bind()
    pending = bind.execute(sa.text("SELECT count(*) FROM media WHERE work_id IS NULL")).scalar_one()
    if pending:
        raise RuntimeError(
            f"还有 {pending} 行 media 的 work_id 是空的，不能收口。\n"
            "先跑 `funflix canon purge --apply` 和 "
            "`funflix canon rebuild --apply` 把归属回填完，再执行这条迁移。"
        )


def _guard_identity_is_restorable() -> None:
    """降级前确认 `uq_media_identity` 能重新加上。"""
    bind = op.get_bind()
    dupes = bind.execute(
        sa.text(
            "SELECT count(*) FROM ("
            "  SELECT 1 FROM media GROUP BY norm_key, media_type, year HAVING count(*) > 1"
            ") AS d"
        )
    ).scalar_one()
    if dupes:
        raise RuntimeError(
            f"有 {dupes} 组 (norm_key, media_type, year) 重复，无法恢复 uq_media_identity。\n"
            "这是预期的 —— 收口之后 norm_key 存的是作品键，一部剧的各季共享它。\n"
            "真要降级，请先自行决定这些行怎么处理（改 norm_key 或删行），再重试。"
        )


def upgrade() -> None:
    _guard_no_unassigned_media()

    # `season` 先把残留的 NULL 填上再设 NOT NULL —— `canon rebuild` 回填的行
    # 都带季号，但迁移 A 之后、rebuild 之前手工插进来的行可能没有。
    # 这里能直接填哨兵值（而 `work_id` 只能报错），因为 0 是「无季概念」的
    # 正确答案；凭空给一行 media 编一个所属作品可不行。
    op.execute(f"UPDATE media SET season = {NO_SEASON} WHERE season IS NULL")

    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.alter_column("work_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.alter_column(
            "season",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=str(NO_SEASON),
        )
        # 顺序要紧：先删旧约束再加新的。反过来的话，那些 norm_key 已经被
        # `canon merge` 刷成作品键的行会在加新约束之前就把旧约束撞穿。
        batch_op.drop_constraint("uq_media_identity", type_="unique")
        batch_op.create_unique_constraint("uq_media_season", ["work_id", "season"])


def downgrade() -> None:
    _guard_identity_is_restorable()

    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.drop_constraint("uq_media_season", type_="unique")
        batch_op.create_unique_constraint("uq_media_identity", ["norm_key", "media_type", "year"])
        batch_op.alter_column(
            "season", existing_type=sa.Integer(), nullable=True, server_default=None
        )
        batch_op.alter_column("work_id", existing_type=sa.Uuid(), nullable=True)
