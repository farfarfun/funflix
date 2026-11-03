"""允许 SSO 用户没有本地密码。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.alter_column(
            "password_hash", existing_type=sa.String(128), existing_nullable=False, nullable=True
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.alter_column(
            "password_hash", existing_type=sa.String(128), existing_nullable=True, nullable=False
        )
