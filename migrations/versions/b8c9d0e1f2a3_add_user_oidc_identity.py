"""给用户绑定 OIDC issuer 和 subject。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("oidc_issuer", sa.String(255), nullable=True))
        batch.add_column(sa.Column("oidc_subject", sa.String(255), nullable=True))
        batch.create_unique_constraint("uq_user_oidc_identity", ["oidc_issuer", "oidc_subject"])


def downgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.drop_constraint("uq_user_oidc_identity", type_="unique")
        batch.drop_column("oidc_subject")
        batch.drop_column("oidc_issuer")
