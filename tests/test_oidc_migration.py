"""OIDC 身份列的迁移在现有 user 表上可升级、可回退。"""

from __future__ import annotations

from pathlib import Path
from runpy import run_path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "migrations/versions/b8c9d0e1f2a3_add_user_oidc_identity.py"
    )
)


def test_oidc_identity_migration_round_trip() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.UniqueConstraint("username", name="uq_user_username"),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            MIGRATION["upgrade"]()
    inspector = sa.inspect(engine)
    assert {"oidc_issuer", "oidc_subject"} <= {c["name"] for c in inspector.get_columns("user")}
    assert any(
        c["name"] == "uq_user_oidc_identity"
        and c["column_names"] == ["oidc_issuer", "oidc_subject"]
        for c in inspector.get_unique_constraints("user")
    )

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            MIGRATION["downgrade"]()
    assert {"oidc_issuer", "oidc_subject"}.isdisjoint(
        c["name"] for c in sa.inspect(engine).get_columns("user")
    )
    engine.dispose()
