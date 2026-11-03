"""现有用户表升级后能保存无本地密码的 SSO 用户。"""

from __future__ import annotations

from pathlib import Path
from runpy import run_path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "migrations/versions/c9d0e1f2a3b4_allow_passwordless_sso_users.py"
    )
)


def test_passwordless_sso_migration_round_trip() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    user = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("oidc_issuer", sa.String(255)),
        sa.Column("oidc_subject", sa.String(255)),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(user.insert().values(id=1, username="local", password_hash="hash"))
        with Operations.context(MigrationContext.configure(connection)):
            MIGRATION["upgrade"]()
        assert next(
            column
            for column in sa.inspect(connection).get_columns("user")
            if column["name"] == "password_hash"
        )["nullable"]
        connection.execute(user.insert().values(id=2, username="sso", password_hash=None))
        connection.execute(user.delete().where(user.c.id == 2))
        with Operations.context(MigrationContext.configure(connection)):
            MIGRATION["downgrade"]()
        assert not next(
            column
            for column in sa.inspect(connection).get_columns("user")
            if column["name"] == "password_hash"
        )["nullable"]
    engine.dispose()
