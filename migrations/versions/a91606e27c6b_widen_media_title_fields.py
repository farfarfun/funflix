"""widen media title/norm_key/original_title

Revision ID: a91606e27c6b
Revises: c4d5e6f7a8b9
Create Date: 2026-08-28 11:00:00.000000

生产环境 parse 落库时命中 StringDataRightTruncationError：抓取到的原始标题
（多集合并列出的资源名、混入大量噪声词的标题等）超过了 VARCHAR(255)。
title/norm_key 放宽到 500；norm_key 参与 uq_media_identity 唯一索引，不能
无限放大，500 已经远超正常剧名长度。original_title 不参与任何索引，直接
放开到 TEXT。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a91606e27c6b"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.alter_column(
            "title",
            existing_type=sa.String(length=255),
            type_=sa.String(length=500),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "norm_key",
            existing_type=sa.String(length=255),
            type_=sa.String(length=500),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "original_title",
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("media", schema=None) as batch_op:
        batch_op.alter_column(
            "original_title",
            existing_type=sa.Text(),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "norm_key",
            existing_type=sa.String(length=500),
            type_=sa.String(length=255),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "title",
            existing_type=sa.String(length=500),
            type_=sa.String(length=255),
            existing_nullable=False,
        )
