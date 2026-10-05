# 开发手册

面向给 funflix 贡献代码的开发者；只想用这个工具的话看 [README.md](../README.md)。

## 常用命令

```bash
uv sync --extra dev

uv run pytest              # 测试
uv run ruff check .        # lint
uv run ruff format .       # 格式化

# 改了模型后生成迁移
uv run alembic revision --autogenerate -m "描述"
```

## 本地安装与发布

组织约定（SPEC §4.4）用 `funbuild` 做构建、版本递增、发布与打标签，不手写发布脚本。
它是**独立安装的开发者工具**，不在本项目的 `dependencies` 或 `dev` extra 里 ——
只用 funflix 的人不需要它，`pip install funflix` 就够了。

```bash
pip install funbuild        # 或 uv tool install funbuild

funbuild install            # 本地构建并安装，清理旧构建，反映当前工作树的代码
funbuild build              # 正式发布（递增版本 → 构建 → 校验 → 上传 → 打标签）
```

发布后记得在 [CHANGELOG.md](../CHANGELOG.md) 补上这个版本的条目（SPEC §14.3：
按版本倒序，只记真实发布过的版本）。

## 跑 PostgreSQL 那部分测试

默认测试全在 SQLite 上，走的是 `LikeSearchBackend`；而**生产上真正跑的是
`PgTrgmSearchBackend`**，两者的关键词子句一行代码都不共用。所以 SQLite 全绿
并不能说明 PG 上是对的。`tests/test_search_pg.py` 补这一块，默认跳过：

```bash
export FUNFLIX_TEST_PG_URL='postgresql+asyncpg://用户@/库名'
uv run pytest tests/test_search_pg.py
```

其中 `test_keyword_query_uses_the_trgm_index` 断言的是**查询计划**而不是结果。
`similarity(a,b) > 阈值` 和 `a % b` 结果完全一样，只有后者走索引 —— 写错了
结果依旧正确、测试依旧全绿，只是慢几百倍。这种退化只有查执行计划才拦得住。
