"""decouple link_check from resource entirely (independent table)

Revision ID: b2c3d4e5f6a7
Revises: a91606e27c6b
Create Date: 2026-08-29 09:00:00.000000

`link_check` 原来靠 `resource_id`（`ondelete="CASCADE"`）挂在 `resource` 下，
`resource` 被 `db reset` 整表清空重建时，校验历史会跟着被级联删掉。校验历史
是全库成本最高的数据（每条都要真实探测网盘接口），不该因为重解析而丢失，也
不该在数据模型上跟 resource 的生命周期有任何耦合——哪怕是可空外键。

这里把 `link_check` 改成完全独立的表：去掉 `resource_id` 列和外键，只按
`(provider, share_id)` 锚定身份（`resource` 表里 `(provider, share_id)` 本来
就是全局去重锚点，而不是 `url` 字符串，同一分享的 URL 写法会变），另外补一列
`url` 存探测当时的地址快照，供人工核对或 resource 行已不存在时溯源。重新解析
出同样身份的资源后，用 `funflix db relink-checks`
（`services/maintenance.relink_checks`）按身份把历史恢复到新 resource 上。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a91606e27c6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_ENUM = sa.Enum(
    "quark",
    "uc",
    "alipan",
    "baidu",
    "pan115",
    "lanzou",
    "tianyi",
    "xunlei",
    "magnet",
    "other",
    name="provider",
    native_enum=False,
    length=32,
)

_RESOURCE_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    with op.batch_alter_table("link_check", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider", _PROVIDER_ENUM, nullable=True))
        batch_op.add_column(sa.Column("share_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("url", sa.String(length=2048), nullable=True))

    # 现存行的 resource_id 此时还是 NOT NULL，全部能关联到 resource，直接回填。
    op.execute(
        "UPDATE link_check SET "
        "provider = (SELECT resource.provider FROM resource "
        "WHERE resource.id = link_check.resource_id), "
        "share_id = (SELECT resource.share_id FROM resource "
        "WHERE resource.id = link_check.resource_id), "
        "url = (SELECT resource.url FROM resource "
        "WHERE resource.id = link_check.resource_id)"
    )

    with op.batch_alter_table("link_check", schema=None) as batch_op:
        batch_op.alter_column(
            "provider", existing_type=_PROVIDER_ENUM, existing_nullable=True, nullable=False
        )
        batch_op.alter_column(
            "share_id",
            existing_type=sa.String(length=255),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "url", existing_type=sa.String(length=2048), existing_nullable=True, nullable=False
        )
        batch_op.drop_constraint(
            batch_op.f("fk_link_check_resource_id_resource"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_link_check_resource_time"))
        batch_op.drop_column("resource_id")
        batch_op.create_index(
            "ix_link_check_identity_time", ["provider", "share_id", "checked_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("link_check", schema=None) as batch_op:
        batch_op.drop_index("ix_link_check_identity_time")
        batch_op.add_column(sa.Column("resource_id", _RESOURCE_ID_TYPE, nullable=True))
        batch_op.create_index(
            batch_op.f("ix_link_check_resource_time"), ["resource_id", "checked_at"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_link_check_resource_id_resource"),
            "resource",
            ["resource_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # 按身份把 resource_id 找回来；找不到匹配 resource 的历史行在降级后就是孤儿
    # （resource_id 仍是 NULL）——降级本来就是有损操作，回不到最初的强耦合状态。
    op.execute(
        "UPDATE link_check SET resource_id = ("
        "SELECT resource.id FROM resource "
        "WHERE resource.provider = link_check.provider "
        "AND resource.share_id = link_check.share_id)"
    )

    with op.batch_alter_table("link_check", schema=None) as batch_op:
        batch_op.alter_column(
            "resource_id", existing_type=_RESOURCE_ID_TYPE, existing_nullable=True, nullable=False
        )
        batch_op.drop_column("url")
        batch_op.drop_column("share_id")
        batch_op.drop_column("provider")
