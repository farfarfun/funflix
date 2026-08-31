# funflix

影视资源分享文本的结构化采集、解析与网盘链接校验。

完整设计见 [docs/DESIGN.md](docs/DESIGN.md)，已知待优化项见 [docs/TODO.md](docs/TODO.md)，
给本项目贡献代码见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

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
funbuild install   # 本地构建并安装，清理旧构建、反映当前代码（生产发布用 funbuild build）

# 建库
alembic upgrade head

# 登记一个 Telegram 频道并采集一次
funflix source add https://t.me/s/<频道名>
funflix source collect
funflix source list

# 起服务（前台，开发用；默认端口 18810）
funflix server run --reload
# 接口文档 http://127.0.0.1:18810/docs

# 后台常驻、状态查询、停止、重启（生产用，本地一样适用）
funflix server start
funflix server status
funflix server stop
funflix server restart
```

## 命令行

| 命令 | 执行的环节 |
| --- | --- |
| `funflix collect` | 采集 |
| `funflix parse` | 解析，默认处理到清空为止，可用 `--limit` 显式设上限 |
| `funflix verify` | 校验，默认处理到清空为止，可用 `--limit` 显式设上限 |
| `funflix run` | 采集 → 解析（`--skip-collect` 时只解析，均不含校验），解析默认处理到清空为止 |
| `funflix worker --once` | 采集 → 解析 → 校验，各推进到队列清空后退出 |
| `funflix worker` / `funflix server start`/`run`（`FUNFLIX_WORKER_ENABLED=true`） | 循环反复：采集 → 解析 → 校验，各推进到队列清空，直到停止（某一队列大量积压时会在这一轮里暂时独占，属预期行为） |

`parse`/`verify` 内部按 `--batch-size`（默认 500）分批拉取执行，不会一次性把全部
待处理行读进内存；进度条从一开始就按总量显示，过程中持续推进。每批内部再按
`--concurrency` 个并发任务处理，每个任务用独立数据库连接与事务（提交粒度因此
是"每条一提交"）。`parse` 默认 `--concurrency 20`，`verify` 默认 `--concurrency 8`——
两者默认值不同是因为风险不对称：`parse` 的瓶颈纯粹是远程数据库往返延迟，多开
并发没有副作用，上限只需留在数据库连接池容量（`pool_size=10` + `max_overflow=20`
＝ 30）以内；`verify` 的并发受限于对网盘的探测频率（见下方限速取舍），盲目调高
没有额外收益。`parse` 在并发下，两条文档若抽出同一部作品会撞上
`Media`/`Resource`/`Tag` 的唯一约束，此时按"被并发抢跑"静默回滚重试、不计入
`parse_attempts`，留到下次 `funflix parse` 自然捞到，不会被误判成真正失败；
`verify` 没有这个问题（按已知 `resource.id` 操作，不存在先查后建）。

`verify` 的默认 `--rate` 已从 1/秒提到 5/秒——见下方「网盘校验」一节的取舍说明。

以上命令都会每隔 `FUNFLIX_WORKER_PROGRESS_SECONDS`（默认 5，`<=0` 关闭）秒打一行
`采集[待处理/处理中/已完成] 解析[...] 校验[...]` 心跳，内容是**数据库里全局的三阶段快照**，
与当前命令具体在跑哪个环节无关；命令自身的处理进度仍看 tqdm 进度条
（`采集 N/total 源`、`解析 N/total 条` 等）。

`db reset` / `db retag` / `db requeue`、`ingest` 是一次性批量操作，没有逐条推进的过程，不在此列。

### 全部命令

顶层命令：

| 命令 | 说明 |
| --- | --- |
| `funflix status` | 查看流水线各环节的记录数（`--verbose` 展开采集源明细） |
| `funflix collect [source_id]` | 采集：把源里的新内容写成原始文本；不传 `source_id` 则采集全部启用的源 |
| `funflix parse` | 抽取：把原始文本解析成作品与资源 |
| `funflix verify` | 校验：探测网盘链接现在还能不能用 |
| `funflix run` | 一条龙：采集全部启用的源，再解析待处理文本 |
| `funflix worker` | 常驻后台 worker：周期性地采集、解析、校验（`--once` 只跑一轮就退出） |
| `funflix server run` | 前台启动 API 服务，Ctrl-C 停止；`--reload` 开发用 |
| `funflix server start` | 后台启动 API 服务：拉一个子进程跑 `server run` |
| `funflix server stop` | 停止后台服务（`SIGTERM` 优雅退出） |
| `funflix server restart` | 先 `stop` 再 `start` |
| `funflix server status` | 查看后台服务是否在跑、PID、安装的版本号 |

`server` 各命令默认监听 `127.0.0.1:18810`，`--host`/`--port`/`--config` 可覆盖；
`--config` 缺省时读 `${XDG_CONFIG_HOME:-~/.config}/farfarfun/funflix/config.toml`
（不存在就用默认值，不算错误）。`start` 写的 PID 文件（`server.pid`）和日志
（`server.log`）都放在同一个配置目录下，跟 `--config` 默认路径统一管理。
| `funflix probes` | 列出可用的网盘校验探针 |
| `funflix extractors` | 列出可用的抽取器 |
| `funflix search <keyword>` | 按剧名搜索作品及其资源 |
| `funflix doc <doc_id>` | 查看一条原始文本及其解析状态 |
| `funflix ingest <path>` | 从文件导入原始文本（`.txt` / `.jsonl`） |

`funflix db` 子命令：

| 命令 | 说明 |
| --- | --- |
| `funflix db upgrade` | 执行数据库迁移 |
| `funflix db downgrade <revision>` | 回滚迁移 |
| `funflix db current` | 显示当前数据库版本 |
| `funflix db revision -m "..."` | 按模型变更自动生成迁移脚本 |
| `funflix db reset` | 清空数据表并重建（采集源配置保留，可用 `--keep-documents`/`--keep-cursors` 调整范围） |
| `funflix db retag` | 按当前规则重新归类已有标签，合并重复项 |
| `funflix db requeue` | 把"新支持的网盘"的历史资源放回校验队列 |
| `funflix db info` | 显示当前连接的数据库方言 |

`funflix source` 子命令：

| 命令 | 说明 |
| --- | --- |
| `funflix source add <url>` | 登记一个采集源，类型与标识按 URL 自动识别 |
| `funflix source list` | 列出全部采集源及其水位 |
| `funflix source show <source_id>` | 查看采集源详情 |
| `funflix source set <source_id>` | 修改采集源配置（间隔、翻页上限、水位、标题） |
| `funflix source reset-cursor [source_id]` | 只归零采集水位，已采集的原始文本原样保留；不传则重置全部源 |
| `funflix source enable <source_id>` | 启用采集源 |
| `funflix source disable <source_id>` | 停用采集源 |
| `funflix source remove <source_id>` | 删除采集源（已采集的原始文本会保留） |
| `funflix source types` | 列出当前支持的采集源类型 |
| `funflix source collect [source_id]` | 立即采集一次，等价于顶层 `funflix collect` |

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

> ⚠️ **默认限速（`--rate`/`FUNFLIX_WORKER_VERIFY_RATE`）已从 1/秒提到 5/秒**。
> 打太快可能触发网盘风控，被限流的响应有被误判成"链接失效"、把整库资源
> 误杀的风险（见下方探针的第一原则）——这是权衡校验吞吐后接受的风险，
> 不是默认安全值。观察到误判迹象（`invalid` 占比突然异常升高）时，用
> `--rate` 调回更低的值。

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

开发环境搭建、测试、lint、生成迁移等见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。
