# 开发手册

面向给 funflix 贡献代码的开发者；只想用这个工具的话看 [README.md](../README.md)。

## 常用命令

```bash
pip install -e ".[dev]"

pytest              # 测试
ruff check .        # lint
ruff format .       # 格式化

# 改了模型后生成迁移
alembic revision --autogenerate -m "描述"
```

## 跑 PostgreSQL 那部分测试

默认测试全在 SQLite 上，走的是 `LikeSearchBackend`；而**生产上真正跑的是
`PgTrgmSearchBackend`**，两者的关键词子句一行代码都不共用。所以 SQLite 全绿
并不能说明 PG 上是对的。`tests/test_search_pg.py` 补这一块，默认跳过：

```bash
export FUNFLIX_TEST_PG_URL='postgresql+asyncpg://用户@/库名'
pytest tests/test_search_pg.py
```

其中 `test_keyword_query_uses_the_trgm_index` 断言的是**查询计划**而不是结果。
`similarity(a,b) > 阈值` 和 `a % b` 结果完全一样，只有后者走索引 —— 写错了
结果依旧正确、测试依旧全绿，只是慢几百倍。这种退化只有查执行计划才拦得住。
