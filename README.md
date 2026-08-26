# funflix

影视资源分享文本的结构化采集、解析与网盘链接校验。

完整设计见 [docs/DESIGN.md](docs/DESIGN.md)。

## 流水线

```
source ──采集──> raw_document ──LLM 抽取──> extraction
（频道/水位）      （原始文本）                  │
                                              ▼
                                    media + resource ──校验──> link_check
                                   （作品）  （网盘链接）
```

每一层各自幂等、可单独重跑。当前进度：

| 阶段 | 状态 |
| --- | --- |
| M0 采集源 + Telegram 采集器 + 水位 | ✅ 已完成 |
| M1 数据模型 + 迁移 + 原始文本接口 | ✅ 已完成 |
| M2 链接扫描 + 文本分段 + 剧名归一 | ✅ 已完成 |
| M3 LLM 抽取 | ✅ 已完成 |
| M4 网盘校验（夸克 / UC / 阿里云盘，匿名探针） | ✅ 已完成 |
| M5 worker 租约 + 常驻调度 | ✅ 已完成 |
| M6 查询接口 | ✅ 已完成（SQLite 走 LIKE，FTS5 后端待补） |
| M7 其余网盘探针（百度 / 蓝奏 / 天翼等） | 待开发 |

## 快速开始

```bash
pip install -e ".[dev]"

# 建库
alembic upgrade head

# 登记一个 Telegram 频道并采集一次
funflix source add https://t.me/s/<频道名>
funflix source collect
funflix source list

# 起服务
funflix serve --reload
# 接口文档 http://127.0.0.1:8000/docs
```

## 配置

所有配置项走 `FUNFLIX_` 前缀的环境变量或 `.env`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FUNFLIX_DATABASE_URL` | `sqlite+aiosqlite:///./funflix.db` | 切 PG 改成 `postgresql+asyncpg://...` |
| `FUNFLIX_ADMIN_API_KEY` | 空 | 管理接口的 key，不配则管理接口关闭 |
| `FUNFLIX_LOG_LEVEL` | `INFO` | |
| `FUNFLIX_INGEST_MAX_BATCH` | `200` | 单批提交上限 |
| `FUNFLIX_INGEST_MAX_CONTENT_LENGTH` | `100000` | 单条文本长度上限 |

## 接口

### 采集源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/sources` | 登记采集源，只给 `url` 即可自动识别类型与标识 |
| `GET` | `/api/v1/sources` | 列表 |
| `GET` | `/api/v1/sources/supported` | 当前支持的源类型 |
| `PATCH` | `/api/v1/sources/{id}` | 改配置；回拨 `cursor_message_id` 即可重采历史 |
| `POST` | `/api/v1/sources/{id}/collect` | 立即采集一次 |
| `DELETE` | `/api/v1/sources/{id}` | 删除（已采文本保留） |

### 原始文本

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/raw` | 提交一条；命中 `content_hash` 返回 `duplicated=true` |
| `POST` | `/api/v1/raw/bulk` | 批量提交 |
| `GET` | `/api/v1/raw` | 按状态 / 来源翻页，不返回全文 |
| `GET` | `/api/v1/raw/{id}` | 详情，含全文 |

### 查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/media` | 搜索 / 浏览作品，支持 `keyword`、`media_type`、`year`、`valid_only` 与翻页 |
| `GET` | `/api/v1/media/{id}` | 作品详情，含全部网盘资源与标签 |
| `GET` | `/api/v1/resources` | 按 `provider` / `check_status` 翻页看链接 |
| `GET` | `/api/v1/resources/{id}` | 单条资源 |
| `GET` | `/api/v1/stats` | 流水线各环节记录数与分布（`funflix status` 的 HTTP 版）|

`/media` 的关键词匹配走 `services/search.py` 的后端抽象：PostgreSQL 上用 `pg_trgm`
容错匹配并按相似度排序，其余方言回落 `LIKE`，调用方无感知。

PostgreSQL 上的三条实测结论（`tests/test_search_pg.py`，5 万行）：

| | 结果 |
| --- | --- |
| 3 字以上关键词 | 走 GIN 位图索引，**0.235ms** |
| 2 个汉字的关键词 | 全表扫描 **81.9ms**，无解 —— pg_trgm 要三元组，两个字提不出完整 trigram |
| 刚批量导入完 | 暂时走不了索引，见下 |

> 📌 **批量导入后记得 VACUUM**。GIN 索引默认开着 fastupdate，新行先进一个待合并
> 列表；合并前规划器认为索引很贵（实测位图扫描启动代价 2515 vs 合并后 64），
> 于是绕开它走全表扫描。autovacuum 会自动处理，赶时间就手动
> `VACUUM ANALYZE media;`。

> ⚠️ DESIGN §7.3 还规划了 `SqliteFtsBackend`（FTS5 虚拟表），目前**尚未实现**。
> 也就是说 SQLite 部署上关键词搜索仍是 `LIKE %x%` 全表扫描 —— 几千条无所谓，
> 上万条就会明显变慢。数据量起来之前先用 PostgreSQL，或者补上 FTS5 后端。

### 鉴权

写接口（`POST`/`PATCH`/`DELETE /sources`、`/sources/{id}/collect`）需要 `X-API-Key`：

```bash
export FUNFLIX_ADMIN_API_KEY=$(openssl rand -hex 32)
curl -X POST localhost:8000/api/v1/sources \
     -H "X-API-Key: $FUNFLIX_ADMIN_API_KEY" \
     -d '{"url": "https://t.me/s/某频道"}'
```

未配置该变量时管理接口一律返回 403（默认关闭比默认放行安全）。CLI 不走 HTTP，不受影响。

> ⚠️ 查询接口目前仍是开放的，其中 `/resources` 会成页返回网盘链接与**提取码**，
> 整库可在 `总数/200` 次请求内翻完。要暴露到公网的话，先给它也加上鉴权或限流。

## 网盘校验

当前有匿名探针的网盘：**夸克、UC、阿里云盘**。其余 provider 的链接照常入库，
只是标成 `unsupported`、不做校验。

UC 与夸克是同一套接口（连业务码都一样），所以 UC 探针直接复用夸克的码表。

> 📌 **新增探针后要跑一次 `funflix db requeue`**。链接落库时若该 provider
> 还没有探针，会被写成 `unsupported` 且不排复查时间，之后即使加了探针也
> **永远不会被领取** —— 新链接正常校验、老链接静默地一直停在 unsupported。

探针的第一原则是**判不出来就归 ERROR，绝不归 INVALID**。接口改版、被风控、
返回一段 HTML 错误页，任何一种被判成"链接失效"，都会在一轮复查里把整库资源
误杀，而且看起来完全正常（状态是"已确认失效"，不是报错）。
`AnonymousHttpProbe` 把这条规则做成了默认行为：`classify` 返回 `None` 即表示
看不懂，骨架翻译成 ERROR —— 新写探针的人**忘了写兜底分支也是安全的**。

## 设计要点

- **水位用消息 ID 而不是时间**。ID 单调且精确，不受时钟漂移、同秒多条消息、消息编辑改时间戳的影响。
- **水位按「见到的最大 ID」推进**，不是「成功落库的最大 ID」。否则一条无正文的纯图片消息会永久卡住采集。
- **首次接入只取最新一页**。想补历史就显式回拨 `cursor_message_id`，避免接入老频道时把整个历史拉下来。
- **入口按 `content_hash` 去重**。同一条分享被多个源重复抓到时在这里挡住，不会走到后面按次计费的 LLM 抽取。
- **时间统一 UTC-aware**。`UTCDateTime` 抹平了 SQLite（读回 naive）与 PostgreSQL（读回 aware）的行为差异，写入 naive 会直接报错。

## 开发

```bash
pytest              # 测试
ruff check .        # lint
ruff format .       # 格式化

# 改了模型后生成迁移
alembic revision --autogenerate -m "描述"
```

### 跑 PostgreSQL 那部分测试

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
