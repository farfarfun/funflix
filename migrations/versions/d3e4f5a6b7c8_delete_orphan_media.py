"""delete media without resources

Revision ID: d3e4f5a6b7c8
Revises: aa9fe8aee9ae
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "aa9fe8aee9ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM media_tag WHERE media_id IN "
            "(SELECT media.id FROM media WHERE NOT EXISTS "
            "(SELECT 1 FROM media_resource WHERE media_resource.media_id = media.id))"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM media WHERE NOT EXISTS "
            "(SELECT 1 FROM media_resource WHERE media_resource.media_id = media.id)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tag SET media_count = "
            "(SELECT COUNT(*) FROM media_tag WHERE media_tag.tag_id = tag.id)"
        )
    )


def downgrade() -> None:
    pass
