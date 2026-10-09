"""给 user 加角色、建 invite_code 表、播种默认访客账号

Revision ID: a7b8c9d0e1f2
Revises: e5f6a7b8c9d0
Create Date: 2026-10-09

## 为什么

在这之前站点是半开放的：作品检索/详情三个端点（`GET /works`、
`GET /works/{id}`、`GET /media/{id}`）完全公开，其余接口挂 `CurrentUserDep`
算「运维」区。现在要求**整站都得有口令**，而看站的口令和运维账号要分开。

分开的做法是给 `user` 加一级角色而不是另起一张表：登录态、停用、密码哈希这些
机制完全一样，拆两张表只是把账号逻辑里每个函数都写两遍。

两张表的列定义来自 `funauth` 的 `UserMixin` / `InviteCodeMixin`（见
`funflix/models/user.py`、`invite.py`），但这条迁移里的 DDL 是写死的 ——
迁移记录的是**当时**的库长什么样，不能跟着依赖升级一起漂。

## 存量账号为什么一律升成 admin

`user` 表里现有的行全是 `funflix user create` 建出来的运维账号 —— 那条命令在这
之前只有这一种用途。backfill 成 `guest` 会把运维自己锁在运维区外面，而且是在
迁移跑完、下一次登录时才发现。

反过来，**列的 `server_default` 取 `guest`**：绕过 ORM 的手写 INSERT 落在最小
权限上，漏填一个字段不该凭空多一个管理员。两个值方向不同是故意的，一个管存量
一个管将来。

## 为什么用 batch_alter_table

只为了改 `nullable`。SQLite 的原生 `ALTER TABLE` 不支持改列的可空性，batch 模式
会退化成「建新表→拷数据→换名」。生产库是 Postgres，走的是原生 ALTER，batch 只是
让这条迁移在 SQLite 上也能跑通（本地/CI 的轻量环境用得到）。

## 播种出来的那行为什么 downgrade 不删

`downgrade()` 删 `invite_code` 表和 `role` 列，但**留着 `funflix` 这个用户行**。
它跑过一轮之后很可能已经被改了密码、甚至被当成某个真人的账号在用，回滚一次
schema 就把它删掉是不可逆的。留着的代价只是多一行用户，自己 `funflix user
disable funflix` 即可。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 默认访客账号。密码在迁移运行时用 `funflix.security.hash_password` 现算 ——
#: 不往这个文件里写死一个哈希字面量，那样所有部署会共用同一个 salt，而且
#: 这个文件是进 git 的。
DEFAULT_GUEST_USERNAME = "funflix"
DEFAULT_GUEST_PASSWORD = "funflix"


def upgrade() -> None:
    op.add_column("user", sa.Column("role", sa.String(16), nullable=True))
    # 存量行全是运维账号，见模块 docstring。
    op.execute("UPDATE \"user\" SET role = 'admin' WHERE role IS NULL")
    with op.batch_alter_table("user") as batch:
        batch.alter_column("role", nullable=False, server_default="guest")

    op.create_table(
        "invite_code",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_invite_code_code"),
    )
    op.create_index("ix_invite_code_created_at", "invite_code", ["created_at"])

    _seed_default_guest()


def _seed_default_guest() -> None:
    """没有 `funflix` 这个用户名时建一个 guest 账号。

    **先判在不在**：让这条迁移可以重复执行（stamp 错了重跑一次不炸），也不会
    覆盖掉用户自己改过的密码 —— 默认口令是给「刚装好、还没人管」用的，一旦
    被改过，迁移就不该再碰它。
    """
    from funflix.models.base import utcnow, uuid7
    from funflix.security import hash_password

    bind = op.get_bind()
    existing = bind.execute(
        sa.text('SELECT 1 FROM "user" WHERE username = :name'),
        {"name": DEFAULT_GUEST_USERNAME},
    ).first()
    if existing is not None:
        return

    now = utcnow()
    bind.execute(
        sa.text(
            'INSERT INTO "user" (id, username, password_hash, role, is_active,'
            " created_at, updated_at)"
            " VALUES (:id, :name, :pwd, 'guest', :active, :now, :now)"
        ),
        {
            "id": uuid7(),
            "name": DEFAULT_GUEST_USERNAME,
            "pwd": hash_password(DEFAULT_GUEST_PASSWORD),
            "active": True,
            "now": now,
        },
    )


def downgrade() -> None:
    op.drop_index("ix_invite_code_created_at", table_name="invite_code")
    op.drop_table("invite_code")
    with op.batch_alter_table("user") as batch:
        batch.drop_column("role")
    # 播种出来的 funflix 用户行故意留着，见模块 docstring。
