"""move id (and PK/FK identity columns) back to the front of the physical column order

Revision ID: f6a7b8c9d0e1
Revises: e7f8a9b0c1d2
Create Date: 2026-08-30 09:00:00.000000

上一次迁移（`e7f8a9b0c1d2`）用"影子列"手法把 `id`/FK 列从 bigint 切成
uuid：`ADD COLUMN xxx_new` 只能把新列追加到表尾，改名回原名后，物理列序就变成
"其余列在前、id（以及 FK 列）在最后"，看着别扭，但功能完全不受影响
（分页/排序/外键完整性都跟物理列序无关）。这里纯粹是外观整理：把 `id` 挪回
物理第一列。

Postgres 没有原生的"改列物理位置"语法（`ALTER TABLE` 只能在表尾追加列），
唯一办法是整表重建：建一张列序正确的新表 → 把数据拷过去 → 删旧表、改新表名
→ 重建 PK/FK/UNIQUE/索引。为了不用手工照抄每张表的建表语句（容易在类型/长度上
打字打错），这里在迁移运行时用 `sa.Table(..., autoload_with=bind)` 反射出当前
真实的列定义，只调整列的排列顺序，类型/可空性一律照抄反射结果；约束和索引则
直接用 `pg_get_constraintdef` / `pg_indexes.indexdef` 现取现建，保证跟重建前
一模一样，不用在这个文件里手写一遍全部约束定义。

只对 PostgreSQL 生效：本地 SQLite 走 `Base.metadata.create_all` 建表，
模型里 `id` 本来就声明在最前面，不存在这个问题。

`media_resource`/`media_tag` 没有自己的 `id`（复合主键），不在整理范围内；
但它们各自两条指向 `media`/`resource`/`tag` 的外键会在这两张父表被重建时
被 `DROP TABLE ... CASCADE` 顺带删掉，所以也要在重建收尾时一并补回去。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 7 张有自己 id 列的表。media_resource/media_tag 是纯关联表，没有 id，不在此列。
_TABLES = ("source", "raw_document", "media", "resource", "tag", "link_check", "extraction")


def _bare_column(col: sa.Column) -> sa.Column:
    """只留类型/可空性/DB 侧默认值，去掉 PK/FK/unique/index 等约束标记 ——
    这些约束单独用 `ADD CONSTRAINT`/`CREATE INDEX` 补，避免建表语句里内联生成
    一份没有命名约定、跟后面手动补的约束重名冲突的隐式约束。"""
    return sa.Column(
        col.name,
        col.type,
        nullable=col.nullable,
        server_default=col.server_default,
        comment=col.comment,
    )


def _capture(bind: sa.engine.Connection, table: str) -> dict:
    # contype 是 pg_constraint 里的内建 "char" 类型——asyncpg 把它读成 bytes
    # （如 b'p'），跟字符串字面量 "p" 比较永远是 False，会静默漏判整类约束；
    # 显式 ::text 转成字符串，不依赖驱动的类型映射。
    constraints = bind.execute(
        sa.text(
            "SELECT conname, contype::text, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE conrelid = cast(:t as regclass)"
        ),
        {"t": table},
    ).fetchall()
    pk = [(c[0], c[2]) for c in constraints if c[1] == "p"]
    fk = [(c[0], c[2]) for c in constraints if c[1] == "f"]
    uq = [(c[0], c[2]) for c in constraints if c[1] == "u"]
    constraint_names = {c[0] for c in constraints}

    indexes = bind.execute(
        sa.text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
        {"t": table},
    ).fetchall()
    plain_indexes = [(i[0], i[1]) for i in indexes if i[0] not in constraint_names]

    reverse_fks = bind.execute(
        sa.text(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE contype = 'f' AND confrelid = cast(:t as regclass)"
        ),
        {"t": table},
    ).fetchall()
    external_child_fk = [r for r in reverse_fks if r[0] not in _TABLES]

    return {
        "pk": pk,
        "fk": fk,
        "uq": uq,
        "indexes": plain_indexes,
        "external_child_fk": external_child_fk,
    }


def _rebuild(bind: sa.engine.Connection, table: str, *, id_first: bool) -> None:
    reflected = sa.Table(table, sa.MetaData(), autoload_with=bind, resolve_fks=False)
    id_col = reflected.columns["id"]
    other_cols = [c for c in reflected.columns if c.name != "id"]

    ordered = [id_col, *other_cols] if id_first else [*other_cols, id_col]
    tmp_name = f"{table}__reorder"
    tmp_table = sa.Table(tmp_name, sa.MetaData(), *(_bare_column(c) for c in ordered))
    bind.execute(sa.schema.CreateTable(tmp_table))

    col_list_sql = ", ".join(f'"{c.name}"' for c in reflected.columns)
    bind.execute(
        sa.text(f'INSERT INTO "{tmp_name}" ({col_list_sql}) SELECT {col_list_sql} FROM "{table}"')
    )
    bind.execute(sa.text(f'DROP TABLE "{table}" CASCADE'))
    bind.execute(sa.text(f'ALTER TABLE "{tmp_name}" RENAME TO "{table}"'))

    # PG18+ 把列级 NOT NULL 也存成命名约束，建表时自动按当时的表名生成
    # （如 "{tmp_name}_id_not_null"）；改表名不会跟着改这些约束名，得手动纠正，
    # 否则会重新制造一遍"名字带着历史包袱"的问题——这正是这次迁移想清理掉的。
    stale = bind.execute(
        sa.text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = cast(:t as regclass) AND contype = 'n' "
            "AND conname LIKE :prefix"
        ),
        {"t": table, "prefix": f"{tmp_name}\\_%"},
    ).fetchall()
    for (conname,) in stale:
        fixed = table + conname[len(tmp_name) :]
        bind.execute(sa.text(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{conname}" TO "{fixed}"'))


def _restore_constraints(bind: sa.engine.Connection, captured: dict[str, dict]) -> None:
    # 先补全部主键——外键要引用父表主键，必须先于任何外键存在。
    for table in _TABLES:
        for conname, condef in captured[table]["pk"]:
            bind.execute(sa.text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{conname}" {condef}'))

    for table in _TABLES:
        for conname, condef in captured[table]["fk"]:
            bind.execute(sa.text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{conname}" {condef}'))
        for child_table, conname, condef in captured[table]["external_child_fk"]:
            bind.execute(
                sa.text(f'ALTER TABLE "{child_table}" ADD CONSTRAINT "{conname}" {condef}')
            )

    for table in _TABLES:
        for conname, condef in captured[table]["uq"]:
            bind.execute(sa.text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{conname}" {condef}'))
        for _indexname, indexdef in captured[table]["indexes"]:
            bind.execute(sa.text(indexdef))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    captured = {table: _capture(bind, table) for table in _TABLES}
    for table in _TABLES:
        _rebuild(bind, table, id_first=True)
    _restore_constraints(bind, captured)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    captured = {table: _capture(bind, table) for table in _TABLES}
    for table in _TABLES:
        _rebuild(bind, table, id_first=False)
    _restore_constraints(bind, captured)
