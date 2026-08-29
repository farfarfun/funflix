"""switch primary keys (and their FKs) from autoincrement bigint to UUIDv7

Revision ID: e7f8a9b0c1d2
Revises: b2c3d4e5f6a7
Create Date: 2026-08-29 12:00:00.000000

远端 Aliyun RDS 到 GitHub Actions runner 的地理延迟没法根治（runner 位置不可控），
后续要引入本地库做拉取/推送同步，多台机器各自写入会撞自增整数主键。这里把全部
7 张有自有主键的表（`source`/`media`/`tag`/`link_check`/`raw_document`/
`resource`/`extraction`）连同它们的 FK 列，从 `BIGINT` 切到 `uuid`，根治多机
ID 冲突——具体动机见 `models/base.py::uuid7` 与本次迁移对应的规划文档。

只对 PostgreSQL 生效：本地 SQLite 走 `Base.metadata.create_all` 全新建表，不
经过这套迁移逻辑；`gen_random_uuid()`/临时 plpgsql 函数都是 PG 专属能力，不用
兼容 SQLite。

**保留 `order_by(id.desc())` = "最新优先" 的语义**：`api/v1/resources.py` 等列
表接口和搜索都靠主键单调递增做分页/排序；纯随机 UUID（`gen_random_uuid()`）会
把这批历史行的相对顺序打乱。所以这里没有直接用 `gen_random_uuid()`，而是现场
建一个临时 SQL 函数 `_uuid7_from_ts(ts)`，按 RFC 9562 UUIDv7 的位布局
（48 位毫秒时间戳前缀 + version/variant + 随机位）从每一行的
`created_at`（`link_check` 用 `checked_at`）现算一个 UUID——效果上等价于
"如果当初建这行时用的就是 `models/base.py::uuid7()`"，迁移完 `id.desc()` 排序
跟迁移前按插入时间排序的结果一致，下游代码不用改一行。

步骤分三阶段，而不是按表逐个"加列→切列→建约束"穿插着做：
1. 每张表先加一个 `xxx_new` 影子列，回填好值（自身 id 用 `_uuid7_from_ts`，
   FK 列按旧 id 连父表拿父表已经回填好的 `id_new`）——这一步全程新旧列共存，
   不影响任何约束。
2. 一次性删掉全部会挡着"删旧列"的约束（引用旧 id 的外键、建在旧 FK 列上的
   索引/唯一约束、旧主键本身），再统一删旧列、把 `xxx_new` 改名回 `xxx`。
   如果按表处理，会撞上"父表 `id` 被子表外键引用，删不掉"的依赖顺序问题；
   全部约束先清空再统一改列，就不用手动排 FK 依赖顺序。
3. 按新列重建主键、外键、以及第 2 步顺带删掉的索引/唯一约束。

降级（`downgrade`）是有损操作：新 `id` 按 UUID（即原插入顺序）用
`row_number()` 重新分配 bigint，相对顺序保留，但具体数值不会等于迁移前的旧
值；同时补回 `BIGSERIAL` 等价的序列，让降级后的表结构跟迁移前一致。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = sa.Uuid(as_uuid=True)
_OLD_ID_TYPE = sa.BigInteger()

#: (表名, 用来现算 UUIDv7 的时间戳列)。link_check 没有 TimestampMixin，
#: 用 checked_at 代替 created_at。
_OWN_ID_TABLES = (
    ("source", "created_at"),
    ("media", "created_at"),
    ("tag", "created_at"),
    ("link_check", "checked_at"),
    ("raw_document", "created_at"),
    ("resource", "created_at"),
    ("extraction", "created_at"),
)

_CREATE_UUID7_FN = """
CREATE FUNCTION _uuid7_from_ts(ts timestamptz) RETURNS uuid AS $$
DECLARE
    ts_hex text := lpad(to_hex(floor(extract(epoch FROM ts) * 1000)::bigint), 12, '0');
    -- 不依赖 pgcrypto（gen_random_bytes 需要装扩展，RDS 上不一定有权限），
    -- 用 md5(random()||clock_timestamp()) 现凑一段十六进制随机串即可，这里只
    -- 要求"足够不重复"，不要求密码学强度。
    rnd_hex text := md5(random()::text || clock_timestamp()::text || ts::text);
    -- 变体位固定为二进制 10xx -> 十六进制第一位落在 8/9/a/b。
    variant_nibble text := (ARRAY['8', '9', 'a', 'b'])[(ascii(substr(rnd_hex, 1, 1)) % 4) + 1];
BEGIN
    RETURN (
        substr(ts_hex, 1, 8) || '-' ||
        substr(ts_hex, 9, 4) || '-' ||
        '7' || substr(rnd_hex, 2, 3) || '-' ||
        variant_nibble || substr(rnd_hex, 5, 3) || '-' ||
        substr(rnd_hex, 8, 12)
    )::uuid;
END;
$$ LANGUAGE plpgsql VOLATILE;
"""

_DROP_UUID7_FN = "DROP FUNCTION _uuid7_from_ts(timestamptz);"


def upgrade() -> None:
    op.execute(_CREATE_UUID7_FN)

    # --- 阶段 1：影子列 + 回填 -------------------------------------------------
    for table, ts_col in _OWN_ID_TABLES:
        op.add_column(table, sa.Column("id_new", _UUID, nullable=True))
        op.execute(f"UPDATE {table} SET id_new = _uuid7_from_ts({ts_col})")
        op.alter_column(table, "id_new", existing_type=_UUID, nullable=False)

    op.add_column("raw_document", sa.Column("source_id_new", _UUID, nullable=True))
    op.execute(
        "UPDATE raw_document rd SET source_id_new = s.id_new "
        "FROM source s WHERE s.id = rd.source_id"
    )

    for table in ("resource", "extraction"):
        op.add_column(table, sa.Column("raw_document_id_new", _UUID, nullable=True))
        op.execute(
            f"UPDATE {table} t SET raw_document_id_new = rd.id_new "
            "FROM raw_document rd WHERE rd.id = t.raw_document_id"
        )
    # extraction.raw_document_id 原本是 NOT NULL，回填后补回这个约束。
    op.alter_column(
        "extraction", "raw_document_id_new", existing_type=_UUID, nullable=False
    )

    op.add_column("media_resource", sa.Column("media_id_new", _UUID, nullable=True))
    op.add_column("media_resource", sa.Column("resource_id_new", _UUID, nullable=True))
    op.execute(
        "UPDATE media_resource mr SET media_id_new = m.id_new, resource_id_new = r.id_new "
        "FROM media m, resource r WHERE m.id = mr.media_id AND r.id = mr.resource_id"
    )
    op.alter_column("media_resource", "media_id_new", existing_type=_UUID, nullable=False)
    op.alter_column(
        "media_resource", "resource_id_new", existing_type=_UUID, nullable=False
    )

    op.add_column("media_tag", sa.Column("media_id_new", _UUID, nullable=True))
    op.add_column("media_tag", sa.Column("tag_id_new", _UUID, nullable=True))
    op.execute(
        "UPDATE media_tag mt SET media_id_new = m.id_new, tag_id_new = t.id_new "
        "FROM media m, tag t WHERE m.id = mt.media_id AND t.id = mt.tag_id"
    )
    op.alter_column("media_tag", "media_id_new", existing_type=_UUID, nullable=False)
    op.alter_column("media_tag", "tag_id_new", existing_type=_UUID, nullable=False)

    op.execute(_DROP_UUID7_FN)

    # --- 阶段 2：清空全部挡着删旧列的约束，再统一切列 ---------------------------
    # 引用旧 id 的外键（都在子表上，跟父表处理顺序无关，可以先一口气全删掉）。
    op.drop_constraint("fk_raw_document_source_id_source", "raw_document", type_="foreignkey")
    op.drop_constraint(
        "fk_resource_raw_document_id_raw_document", "resource", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_extraction_raw_document_id_raw_document", "extraction", type_="foreignkey"
    )
    op.drop_constraint("fk_media_resource_media_id_media", "media_resource", type_="foreignkey")
    op.drop_constraint(
        "fk_media_resource_resource_id_resource", "media_resource", type_="foreignkey"
    )
    op.drop_constraint("fk_media_tag_media_id_media", "media_tag", type_="foreignkey")
    op.drop_constraint("fk_media_tag_tag_id_tag", "media_tag", type_="foreignkey")

    # 建在旧列上的索引/唯一约束，删列前必须先清掉。
    op.drop_index("ix_raw_document_source_msg", table_name="raw_document")
    op.drop_constraint("uq_extraction_doc_model_version", "extraction", type_="unique")
    op.drop_index("ix_media_resource_resource", table_name="media_resource")
    op.drop_index("ix_media_tag_tag", table_name="media_tag")

    # 旧主键本身。
    for table, _ts_col in _OWN_ID_TABLES:
        op.drop_constraint(f"pk_{table}", table, type_="primary")
    op.drop_constraint("pk_media_resource", "media_resource", type_="primary")
    op.drop_constraint("pk_media_tag", "media_tag", type_="primary")

    # 删旧列、影子列改名回原名。
    for table, _ts_col in _OWN_ID_TABLES:
        op.drop_column(table, "id")
        op.alter_column(table, "id_new", new_column_name="id")
    op.drop_column("raw_document", "source_id")
    op.alter_column("raw_document", "source_id_new", new_column_name="source_id")
    for table in ("resource", "extraction"):
        op.drop_column(table, "raw_document_id")
        op.alter_column(table, "raw_document_id_new", new_column_name="raw_document_id")
    for col in ("media_id", "resource_id"):
        op.drop_column("media_resource", col)
        op.alter_column("media_resource", f"{col}_new", new_column_name=col)
    for col in ("media_id", "tag_id"):
        op.drop_column("media_tag", col)
        op.alter_column("media_tag", f"{col}_new", new_column_name=col)

    # --- 阶段 3：重建主键 / 外键 / 索引 -----------------------------------------
    for table, _ts_col in _OWN_ID_TABLES:
        op.create_primary_key(f"pk_{table}", table, ["id"])
    op.create_primary_key("pk_media_resource", "media_resource", ["media_id", "resource_id"])
    op.create_primary_key("pk_media_tag", "media_tag", ["media_id", "tag_id"])

    op.create_foreign_key(
        "fk_raw_document_source_id_source",
        "raw_document",
        "source",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_resource_raw_document_id_raw_document",
        "resource",
        "raw_document",
        ["raw_document_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_extraction_raw_document_id_raw_document",
        "extraction",
        "raw_document",
        ["raw_document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_media_resource_media_id_media",
        "media_resource",
        "media",
        ["media_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_media_resource_resource_id_resource",
        "media_resource",
        "resource",
        ["resource_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_media_tag_media_id_media",
        "media_tag",
        "media",
        ["media_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_media_tag_tag_id_tag", "media_tag", "tag", ["tag_id"], ["id"], ondelete="CASCADE"
    )

    op.create_index(
        "ix_raw_document_source_msg", "raw_document", ["source_id", "source_msg_id"]
    )
    op.create_unique_constraint(
        "uq_extraction_doc_model_version",
        "extraction",
        ["raw_document_id", "model", "prompt_version"],
    )
    op.create_index("ix_media_resource_resource", "media_resource", ["resource_id"])
    op.create_index("ix_media_tag_tag", "media_tag", ["tag_id"])


def downgrade() -> None:
    # 有损：新 bigint id 按 UUID（=原插入顺序）用 row_number() 重新分配，
    # 具体数值跟迁移前的旧值对不上，但相对顺序保留。
    for table, _ts_col in _OWN_ID_TABLES:
        op.add_column(table, sa.Column("id_old", _OLD_ID_TYPE, nullable=True))
        op.execute(
            f"UPDATE {table} t SET id_old = sub.rn FROM "
            f"(SELECT id, row_number() OVER (ORDER BY id) AS rn FROM {table}) sub "
            "WHERE sub.id = t.id"
        )
        op.alter_column(table, "id_old", existing_type=_OLD_ID_TYPE, nullable=False)

    op.add_column("raw_document", sa.Column("source_id_old", _OLD_ID_TYPE, nullable=True))
    op.execute(
        "UPDATE raw_document rd SET source_id_old = s.id_old "
        "FROM source s WHERE s.id = rd.source_id"
    )

    for table in ("resource", "extraction"):
        op.add_column(table, sa.Column("raw_document_id_old", _OLD_ID_TYPE, nullable=True))
        op.execute(
            f"UPDATE {table} t SET raw_document_id_old = rd.id_old "
            "FROM raw_document rd WHERE rd.id = t.raw_document_id"
        )
    op.alter_column(
        "extraction", "raw_document_id_old", existing_type=_OLD_ID_TYPE, nullable=False
    )

    op.add_column("media_resource", sa.Column("media_id_old", _OLD_ID_TYPE, nullable=True))
    op.add_column("media_resource", sa.Column("resource_id_old", _OLD_ID_TYPE, nullable=True))
    op.execute(
        "UPDATE media_resource mr SET media_id_old = m.id_old, resource_id_old = r.id_old "
        "FROM media m, resource r WHERE m.id = mr.media_id AND r.id = mr.resource_id"
    )
    op.alter_column(
        "media_resource", "media_id_old", existing_type=_OLD_ID_TYPE, nullable=False
    )
    op.alter_column(
        "media_resource", "resource_id_old", existing_type=_OLD_ID_TYPE, nullable=False
    )

    op.add_column("media_tag", sa.Column("media_id_old", _OLD_ID_TYPE, nullable=True))
    op.add_column("media_tag", sa.Column("tag_id_old", _OLD_ID_TYPE, nullable=True))
    op.execute(
        "UPDATE media_tag mt SET media_id_old = m.id_old, tag_id_old = t.id_old "
        "FROM media m, tag t WHERE m.id = mt.media_id AND t.id = mt.tag_id"
    )
    op.alter_column("media_tag", "media_id_old", existing_type=_OLD_ID_TYPE, nullable=False)
    op.alter_column("media_tag", "tag_id_old", existing_type=_OLD_ID_TYPE, nullable=False)

    # 清空挡着删 uuid 列的约束。
    op.drop_constraint("fk_raw_document_source_id_source", "raw_document", type_="foreignkey")
    op.drop_constraint(
        "fk_resource_raw_document_id_raw_document", "resource", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_extraction_raw_document_id_raw_document", "extraction", type_="foreignkey"
    )
    op.drop_constraint("fk_media_resource_media_id_media", "media_resource", type_="foreignkey")
    op.drop_constraint(
        "fk_media_resource_resource_id_resource", "media_resource", type_="foreignkey"
    )
    op.drop_constraint("fk_media_tag_media_id_media", "media_tag", type_="foreignkey")
    op.drop_constraint("fk_media_tag_tag_id_tag", "media_tag", type_="foreignkey")

    op.drop_index("ix_raw_document_source_msg", table_name="raw_document")
    op.drop_constraint("uq_extraction_doc_model_version", "extraction", type_="unique")
    op.drop_index("ix_media_resource_resource", table_name="media_resource")
    op.drop_index("ix_media_tag_tag", table_name="media_tag")

    for table, _ts_col in _OWN_ID_TABLES:
        op.drop_constraint(f"pk_{table}", table, type_="primary")
    op.drop_constraint("pk_media_resource", "media_resource", type_="primary")
    op.drop_constraint("pk_media_tag", "media_tag", type_="primary")

    for table, _ts_col in _OWN_ID_TABLES:
        op.drop_column(table, "id")
        op.alter_column(table, "id_old", new_column_name="id")
    op.drop_column("raw_document", "source_id")
    op.alter_column("raw_document", "source_id_old", new_column_name="source_id")
    for table in ("resource", "extraction"):
        op.drop_column(table, "raw_document_id")
        op.alter_column(table, "raw_document_id_old", new_column_name="raw_document_id")
    for col in ("media_id", "resource_id"):
        op.drop_column("media_resource", col)
        op.alter_column("media_resource", f"{col}_old", new_column_name=col)
    for col in ("media_id", "tag_id"):
        op.drop_column("media_tag", col)
        op.alter_column("media_tag", f"{col}_old", new_column_name=col)

    for table, _ts_col in _OWN_ID_TABLES:
        op.create_primary_key(f"pk_{table}", table, ["id"])
        # 补回 BIGSERIAL 等价的序列，让降级后的表结构跟迁移前完全一致。
        op.execute(f"CREATE SEQUENCE {table}_id_seq OWNED BY {table}.id")
        op.execute(
            f"SELECT setval('{table}_id_seq', (SELECT COALESCE(MAX(id), 0) FROM {table}))"
        )
        op.alter_column(
            table, "id", server_default=sa.text(f"nextval('{table}_id_seq'::regclass)")
        )
    op.create_primary_key("pk_media_resource", "media_resource", ["media_id", "resource_id"])
    op.create_primary_key("pk_media_tag", "media_tag", ["media_id", "tag_id"])

    op.create_foreign_key(
        "fk_raw_document_source_id_source",
        "raw_document",
        "source",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_resource_raw_document_id_raw_document",
        "resource",
        "raw_document",
        ["raw_document_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_extraction_raw_document_id_raw_document",
        "extraction",
        "raw_document",
        ["raw_document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_media_resource_media_id_media",
        "media_resource",
        "media",
        ["media_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_media_resource_resource_id_resource",
        "media_resource",
        "resource",
        ["resource_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_media_tag_media_id_media",
        "media_tag",
        "media",
        ["media_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_media_tag_tag_id_tag", "media_tag", "tag", ["tag_id"], ["id"], ondelete="CASCADE"
    )

    op.create_index(
        "ix_raw_document_source_msg", "raw_document", ["source_id", "source_msg_id"]
    )
    op.create_unique_constraint(
        "uq_extraction_doc_model_version",
        "extraction",
        ["raw_document_id", "model", "prompt_version"],
    )
    op.create_index("ix_media_resource_resource", "media_resource", ["resource_id"])
    op.create_index("ix_media_tag_tag", "media_tag", ["tag_id"])
