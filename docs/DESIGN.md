# funflix 设计文档

> 采集网上分享的影视资源文本 → LLM 结构化抽取 → 剧名归一 → 网盘链接有效性校验 → 提供查询接口。

## 1. 技术选型（已确认）

| 维度 | 选择 | 说明 |
| --- | --- | --- |
| Web | FastAPI | 全异步，Pydantic v2 |
| ORM | SQLAlchemy 2.0（Declarative + `Mapped[]`） | 异步 session |
| DB | SQLite 起步，`DATABASE_URL` 可切 PostgreSQL | schema 只用两库共有类型 |
| 迁移 | Alembic（`render_as_batch=True`） | SQLite 的 ALTER 限制 |
| 抽取 | 全量 LLM（Claude，结构化输出） | 每条原始文本一次调用 |
| 网盘 | fundrive 2.0 + 自研 HTTP 探针 | 见 §6 |
| 后台 | 库内状态机 + 租约领取 + `asyncio` 常驻循环 | 见 §5 |
| 凭证 | funsecret（fundrive 原生配置方式） | 不入库不入 git |

### SQLite → PG 的兼容约束

- 用 `sa.JSON`，不用 `JSONB`；PG 上通过 `.with_variant(JSONB, "postgresql")` 自动升级。
- 所有 `DateTime(timezone=True)`，应用侧统一写 UTC-aware。
- 主键统一 `BigInteger` 自增（SQLite 上退化为 INTEGER，Alembic variant 处理）。
- 不用 PG 独有的 `ARRAY` / 部分索引 / `ON CONFLICT ... WHERE`；去重靠普通唯一索引 + 应用层 upsert。
- 模糊搜索：SQLite 走 FTS5 虚拟表；PG 走 `pg_trgm`。抽象成 `SearchBackend` 协议，两套实现，见 §7.3。

---

## 2. 数据流

```
                  ┌────────────────────┐
   Telegram 频道 →│ Collect: 采集      │→ source (持有水位)
   等可持续拉取源  └────────────────────┘        │ 新消息正文
                                                ▼
                  ┌─────────────┐
  文本/爬虫/手工 →│ POST /raw   │→ raw_document (content_hash 去重)
                  └─────────────┘        │ parse_status=pending
                                         ▼
                              ┌────────────────────┐
                              │ Parse: LLM 抽取     │→ extraction (留档 LLM 原始输出)
                              └────────────────────┘
                                         │ items[]
                                         ▼
                              ┌────────────────────┐
                              │ Normalize: 剧名归一 │→ media (作品实体，去重合并)
                              └────────────────────┘
                                         │
                                         ▼
                              ┌────────────────────┐
                              │ Persist: 资源落库   │→ resource (provider+share_id 唯一)
                              └────────────────────┘        │ check_status=unchecked
                                                            ▼
                              ┌────────────────────┐
                              │ Verify: 网盘校验    │→ link_check (历史) + resource 冗余最新态
                              └────────────────────┘
                                         │
                                         ▼
                                  GET /search 等查询接口
```

各阶段各自幂等、各自可单独重跑：
- **重采历史**：把 `source.cursor_message_id` 回拨即可，重复消息由 `content_hash` 挡掉。
- **重跑抽取**：`extraction` 按 `(raw_document_id, model, prompt_version)` 唯一，换 prompt 版本即产生新记录，旧的保留可对比。
- **重跑校验**：`link_check` 只追加，`resource` 上冗余最新结果供查询。

---

## 3. 数据模型

### 3.0 `source` — 采集源

一个 Source 是一个可持续拉取的消息流（如一个 Telegram 频道）。它持有**水位**，
每次采集只取水位之后的新消息，把正文写成 RawDocument 后即结束职责。

| 字段 | 说明 |
| --- | --- |
| `id` | |
| `source_type` | 复用 `SourceType`，采集器注册表按它分发 |
| `url` | 采集源地址，如 `https://t.me/s/<频道名>` |
| `identifier` | 规范化标识（Telegram 即频道名）。同一频道有 `t.me/x`、`t.me/s/x`、`@x` 多种写法，唯一性判定必须基于它而非 url |
| `title` | 展示名，首次采集时自动回填 |
| `enabled` / `fetch_interval_seconds` / `max_pages_per_fetch` | 调度配置 |
| `cursor_message_id` | **主水位**：已采集到的最大消息 ID |
| `cursor_published_at` | 辅助水位，仅供展示与人工核对 |
| `last_fetched_at` / `last_success_at` / `next_fetch_at` / `lease_until` | 调度状态 |
| `consecutive_failures` / `last_error` | 健康度，用于退避与告警 |
| `total_collected` | 累计产出的新 RawDocument 数 |

唯一索引 `(source_type, identifier)` —— 同一个源被登记两次会各持一份水位，把同批消息采两遍。
索引 `(enabled, next_fetch_at)` 供调度器领取。

三个容易踩的水位问题，实现里已处理：

1. **用消息 ID 而不是时间做主水位**。ID 单调且精确；时间会受时钟漂移、
   同秒多条消息、消息编辑改时间戳影响，用它当水位会漏采或重采。
2. **水位按「见到的最大 ID」推进，而不是「成功落库的最大 ID」**。
   无正文的纯图片消息不落库，若不推水位，它会永久卡住采集，每轮重复拉取。
3. **首次采集只取最新一页**。无水位时若一路回溯，接入一个老频道会把整个历史拉下来。
   补历史应显式回拨 `cursor_message_id`，并由 `max_pages_per_fetch` 兜底。

### 3.1 `raw_document` — 原始文本

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BigInt PK | |
| `content` | Text | 原始文本全文，不做任何加工 |
| `content_hash` | String(64) **UNIQUE** | `sha256(normalize_ws(content))`，入口去重 |
| `source_type` | Enum | `telegram` / `weibo` / `forum` / `manual` / `api` |
| `source_name` | String(128) | 频道名 / 站点名 |
| `source_url` | String(1024) | 可空，原帖链接 |
| `source_msg_id` | String(128) | 可空，来源侧消息 ID |
| `published_at` | DateTime(tz) | 可空，原帖发布时间 |
| `collected_at` | DateTime(tz) | 入库时间 |
| `extra` | JSON | 来源侧的任意元信息 |
| `parse_status` | Enum | `pending` / `running` / `done` / `failed` / `skipped` |
| `parse_attempts` | Int | 重试计数 |
| `parse_error` | Text | 最后一次失败原因 |
| `lease_until` | DateTime(tz) | 任务租约，见 §5 |

索引：`content_hash`(uniq)、`(parse_status, lease_until)`、`(source_type, source_name, published_at)`。

### 3.2 `extraction` — LLM 抽取留档

| 字段 | 说明 |
| --- | --- |
| `id`, `raw_document_id` FK | |
| `model` | 如 `claude-sonnet-5` |
| `prompt_version` | 如 `v3`；prompt 改动必须升版本 |
| `output` | JSON，LLM 返回的结构化结果原样 |
| `input_tokens` / `output_tokens` / `latency_ms` | 成本与性能观测 |
| `created_at` | |

唯一索引 `(raw_document_id, model, prompt_version)` → 天然做**结果缓存**，重复提交同一文本不会二次烧 token。

### 3.3 `media` — 归一后的作品实体

| 字段 | 说明 |
| --- | --- |
| `id` | |
| `title` | 展示用主标题 |
| `norm_key` | String(255)，归一键，见 §4.2 |
| `original_title` | 可空，外语原名 |
| `media_type` | `movie` / `tv` / `anime` / `variety` / `documentary` / `unknown` |
| `year` | Int 可空 |
| `aliases` | JSON `list[str]`，收集到的各种叫法 |
| `tmdb_id` / `douban_id` / `imdb_id` | 可空，预留外部富化 |
| `poster_url` / `overview` | 可空 |
| `resource_count` / `valid_resource_count` | 冗余计数，查询列表页用 |
| `created_at` / `updated_at` | |

唯一索引 `(norm_key, media_type, year)`。`year` 为空时用哨兵值 `0` 参与唯一约束（SQLite/PG 对 NULL 在唯一索引里的行为不一致，必须避开 NULL）。

### 3.4 `resource` — 一条网盘资源（核心表）

| 字段 | 说明 |
| --- | --- |
| `id` | |
| `media_id` FK | 可空（归一失败时挂 null，进人工队列） |
| `raw_document_id` FK | 溯源 |
| `provider` | Enum，见 §6 |
| `share_id` | String(255)，从 URL 提取的分享标识 |
| `url` | String(2048)，规范化后的 URL |
| `passcode` | String(32) 可空，提取码 |
| `title_raw` | 原文里这条链接对应的标题片段 |
| `quality` | `4k` / `1080p` / `720p` / `unknown` |
| `episode_info` | String(64)，如 `S01E01-E12` / `全40集` |
| `size_bytes` | BigInt 可空 |
| `check_status` | Enum，见下 |
| `check_attempts` | Int |
| `last_checked_at` / `next_check_at` | DateTime(tz)，复查调度 |
| `first_seen_at` / `last_seen_at` / `seen_count` | 同一链接被多处分享时的热度信号 |
| `lease_until` | 任务租约 |

唯一索引 `(provider, share_id)` — **这是全局去重的锚点**。索引：`(check_status, next_check_at)`、`(media_id, check_status)`。

`CheckStatus`：
`unchecked` → `checking` → `valid` / `invalid`（失效/被删/违规）/ `need_password`（缺提取码）/ `rate_limited`（限流，退避重试）/ `unsupported`（无该网盘校验能力）/ `error`（探针异常）

### 3.5 `link_check` — 校验历史（只追加）

`id`, `resource_id` FK, `checked_at`, `status`, `http_code`, `detail`(Text), `probe`(String，用了哪个探针实现)

保留时序，用于回答"这条链接什么时候挂的""某网盘最近整体失效率"。可按保留期归档。

### 3.6 关系

`raw_document 1─n extraction`、`raw_document 1─n resource`、`media 1─n resource`、`resource 1─n link_check`。

---

## 4. 解析层

### 4.1 LLM 抽取

**凭证来源（已定）**：走 `nltsecret`，不进环境变量也不进代码：

```python
from nltsecret import read_secret

base_url = read_secret("funflix", "llm", "base_url")
api_key = read_secret("funflix", "llm", "api_key")
```

`base_url` 可配意味着走 OpenAI 兼容协议的网关，客户端按该协议实现，模型名单独配。

一条 `raw_document` → 一次调用 → 结构化 JSON。用 tool-use / JSON schema 强制结构：

```jsonc
{
  "items": [
    {
      "title": "剧名（去掉字幕组/清晰度/表情等噪声）",
      "original_title": null,
      "year": 2024,
      "media_type": "tv",
      "episode_info": "全40集",
      "quality": "1080p",
      "links": [
        { "url": "https://pan.quark.cn/s/xxxxxxxx", "passcode": null, "provider_hint": "quark" }
      ]
    }
  ],
  "unmatched_links": ["原文里存在但无法归属到任何标题的链接"]
}
```

关键约束写进 prompt：
- **一条文本可能含多部作品、一部作品可能多个链接** → `items` 是数组，`links` 也是数组。
- URL 必须**逐字照抄原文**，不许改写补全 —— LLM 幻觉 URL 是这个系统最致命的错误。
- 找不到就填 `null`，不许猜。

### 4.2 抽取后的确定性校正（不信任 LLM 的部分）

LLM 出错代价最高的是链接，所以链接走**双轨**：

1. 正则独立扫一遍原文，得到 `regex_links` 集合。
2. LLM 返回的每个 `url` 必须能在原文中 `find()` 到，否则丢弃并记 `hallucinated_url` 指标。
3. `regex_links - llm_links` 的差集进 `unmatched_links`，不丢弃，挂到该文档下待人工/二次归属。

剧名和分类信任 LLM，链接以正则为准。

### 4.3 剧名归一（`norm_key`）

纯确定性函数，可单测：

1. 全角 → 半角，Unicode NFKC。
2. 剥离括号噪声：`[...]`、`【...】`、`(2024)`、`（4K）`。
3. 剥离噪声 token：清晰度（`4K/1080P/HDR/DV/REMUX`）、来源（`WEB-DL/BluRay`）、字幕（`中字/内嵌/双语`）、集数（`全N集/EPxx/S01`）、字幕组署名。
4. 繁体 → 简体（`opencc`，可选依赖，缺失时降级跳过）。
5. 去除所有空白与标点，小写化 → `norm_key`。

归并策略：`(norm_key, media_type, year)` 命中已有 `media` 则复用并把原始标题追加进 `aliases`；否则新建。

---

## 5. 后台任务执行

可靠性放在库里，进程只是执行器。实现在 `funflix/worker/`：

1. **状态即队列**：`parse_status` / `check_status` + `lease_until` + `attempts` 就是任务表。
2. **租约领取**（`worker/claim.py`）：核心是一条**带守卫的 UPDATE** ——

   ```sql
   UPDATE raw_document SET parse_status='running', lease_until=:until
    WHERE id=:id AND <与候选查询完全相同的条件>
   ```

   `rowcount == 1` 才算领到。两个 worker 同时盯上同一行时只有一个能命中，
   另一个拿到 0 行自动跳过。不用 `FOR UPDATE SKIP LOCKED` 是因为 SQLite 不支持，
   而 schema 必须两库通吃（§1）。守卫条件与候选查询共用同一份表达式，避免二者各自演化。
3. **过期租约即补偿**：候选条件同时接受「pending 且无租约」与「running 且租约已过期」，
   崩溃遗留的任务在租约到期后自动回到队列 —— 补偿是领取逻辑的一部分，
   **不需要**单独的启动扫描。启动时只做一次只读体检并打日志。

   刻意不在启动时强清租约：多 worker 部署下，此刻未过期的租约可能正被另一个
   活着的进程持有，清掉它就会造成同一条任务被两个进程同时处理 —— 正是租约要防的事。
4. **毒任务防护**：逐行领取（而非一条 UPDATE 批量领）是为了分辨每一行是「新任务」
   还是「上一个 worker 崩溃后被重捞的任务」。后者计入 `attempts`，够次数后置终态；
   否则一条能让进程崩溃的文档会被无限重捞，worker 起来、崩掉、再起来，永远卡在它上面。
5. **周期扫描**（`worker/scheduler.py`）：`asyncio` 常驻循环，默认每 60s 把
   采集 → 解析 → 校验各推进一批。顺序有意为之：本轮采到的新文本能被本轮解析吃掉，
   产出的资源又能被本轮校验捡走，一轮走完整条流水线。
6. **逃生舱**：`funflix worker` CLI 跑同一套 claim 逻辑，脱离 API 进程独立消费；
   `--once` 只跑一轮。想上 Celery/arq 时只需替换轮询循环，模型层不动。

进程内 worker（`FUNFLIX_WORKER_ENABLED`）**默认关闭**：一是开着的话 `funflix serve`
会自己开始调 LLM、探网盘，一条真实花钱的副作用不该由"起个 API"隐式触发；
二是 uvicorn 多 worker 部署时每个进程都会起一份，租约虽能防重复处理，但白白多出几倍空转。
生产建议用独立的 `funflix worker` 进程。

重试：指数退避 `min(60s * 2^attempts, 6h)`（`base/backoff.py`，三条流水线共用），
解析 `attempts >= 5` 置终态 `failed`。校验不设终态 —— 探测很便宜，靠退避封顶即可。

---

## 6. 网盘校验层

### 6.1 抽象

```python
class LinkRef(NamedTuple):
    provider: Provider
    share_id: str
    url: str
    passcode: str | None


class CheckOutcome(NamedTuple):
    status: CheckStatus
    http_code: int | None
    detail: str
    title: str | None  # 网盘侧返回的资源名，可回填校正
    size_bytes: int | None


class LinkProbe(Protocol):
    provider: Provider
    patterns: tuple[re.Pattern, ...]  # 识别 + 抽 share_id
    needs_auth: bool

    def parse(self, url: str) -> LinkRef | None: ...
    async def check(self, ref: LinkRef) -> CheckOutcome: ...
```

`registry.py` 按 provider 注册，`linkscan.py` 用所有 probe 的 `patterns` 并集扫原文 → 天然做到"新增一个网盘 = 新增一个文件"。

### 6.2 各网盘实现路径

| Provider | 实现 | 是否需登录 |
| --- | --- | --- |
| `quark` / `uc` | **自研 HTTP 探针**：POST share token 接口，看返回码判断 失效/需提取码/正常。fundrive 无原生驱动 | 否（匿名探针足够） |
| `alipan` | fundrive `alipan` 驱动（Aligo / Open API 两种），或匿名 `share_link/get_share_by_anonymous` 接口 | 匿名优先 |
| `baidu` | fundrive `baidu` 驱动；分享页多带提取码，需走 `verify` 再 `list` | 是 |
| `pan115` | fundrive `pan115` 驱动 | 是 |
| `lanzou` | fundrive `lanzou` 驱动 | 否 |
| `tianyi` | 自研探针（fundrive 无驱动） | 是 |
| 其余 | `unsupported`，只入库不校验 | — |

**原则：能匿名探测就绝不登录。** 匿名探针无凭证依赖、无账号风险、可高并发；只有匿名判不出来时才降级到 fundrive 带登录态的驱动。`FundriveProbe` 是个通用适配器，把 `BaseDrive.save_shared()/get_file_list()` 的结果映射成 `CheckOutcome`，新增 fundrive 支持的网盘基本零成本。

### 6.3 防封与限流

- 每 provider 独立**令牌桶**（默认 1 QPS，可配），跨任务共享。
- **单飞（single-flight）**：同一 `(provider, share_id)` 并发校验合并成一次。
- 命中 429/风控 → 该 provider 全局熔断 N 分钟，期间任务标 `rate_limited` 并延后。
- 随机 UA + 请求间抖动。

### 6.4 复查策略

| 当前状态 | 下次复查 |
| --- | --- |
| `valid` | 7 天后 |
| `invalid` | 30 天后再确认一次，连续两次 invalid 则不再复查 |
| `rate_limited` / `error` | 指数退避 |
| `need_password` | 不自动复查，等提取码补充 |

---

## 7. API 设计

前缀 `/api/v1`。

### 7.1 写入

- `POST /raw` — body: `{content, source_type, source_name, source_url?, published_at?, extra?}`；支持数组批量。
  返回 `{id, content_hash, duplicated: bool}`。`duplicated=true` 时直接返回已有记录，不重复消耗 LLM。
- `POST /raw/{id}/reparse` — 强制重跑抽取（可指定 `prompt_version`）。
- `POST /resources/{id}/recheck` — 立即重校验。

### 7.2 查询

- `GET /search` — 主查询接口
  参数：`q`（剧名模糊）、`media_type`、`year`、`provider`、`check_status`（默认只返 `valid`）、`quality`、`sort`（`latest`/`hot`）、`page`/`size`
  返回：按 `media` 聚合，每个 media 带 `resources[]`。
- `GET /media/{id}` — 作品详情 + 全部资源（含失效的，标注状态）。
- `GET /resources/{id}` — 单条资源详情 + 最近几次 `link_check` 历史。
- `GET /raw/{id}` — 原始文本 + 抽取结果，用于排查。

### 7.3 搜索后端抽象

```python
class SearchBackend(Protocol):
    async def search_media(self, q: str, limit: int) -> list[int]: ...
```

- `SqliteFtsBackend`：`media_fts` FTS5 虚拟表，索引 `title + aliases`，触发器同步。
- `PgTrgmBackend`：`pg_trgm` GIN 索引 + `similarity()` 排序。
- `LikeBackend`：兜底，`LIKE %q%`，小数据量够用。

由 `DATABASE_URL` 的方言自动选择。

### 7.4 运维

`GET /healthz`、`GET /api/v1/admin/stats`（各状态计数、各网盘失效率、LLM token 消耗）。管理接口用 API Key header 保护。

---

## 8. 目录结构

```
funflix/
├── pyproject.toml
├── README.md
├── docs/DESIGN.md
├── alembic.ini
├── migrations/versions/
├── funflix/
│   ├── config.py                 # pydantic-settings
│   ├── db.py                     # async engine / session / Base
│   ├── enums.py
│   ├── models/                   # SQLAlchemy 2.0
│   │   ├── raw.py  media.py  resource.py  check.py  extraction.py
│   ├── schemas/                  # Pydantic I/O
│   ├── repository/               # 数据访问，含 claim/lease 逻辑
│   ├── services/
│   │   ├── ingest.py
│   │   ├── parse/
│   │   │   ├── llm.py            # Claude 调用 + 结构化输出
│   │   │   ├── prompts.py        # 带 PROMPT_VERSION 常量
│   │   │   ├── linkscan.py       # 正则扫链接 + URL 规范化
│   │   │   └── pipeline.py       # 抽取→校正→归一→落库
│   │   ├── normalize.py          # norm_key 纯函数
│   │   ├── search/               # SearchBackend 三实现
│   │   └── verify/
│   │       ├── base.py  registry.py  ratelimit.py
│   │       ├── fundrive_probe.py # 通用 fundrive 适配器
│   │       ├── quark.py  alipan.py  baidu.py  lanzou.py  pan115.py  tianyi.py
│   ├── worker/
│   │   ├── claim.py              # 租约领取
│   │   ├── scheduler.py          # asyncio 常驻扫描
│   │   └── tasks.py              # parse_document / check_resource
│   ├── api/
│   │   ├── app.py  deps.py
│   │   └── v1/ raw.py media.py resources.py search.py admin.py
│   └── cli.py                    # typer: import / reparse / recheck / worker
└── tests/
    ├── test_normalize.py         # 剧名归一，表驱动，重点覆盖
    ├── test_linkscan.py          # 各网盘 URL 正则，含畸形样例
    ├── test_probes.py            # respx mock HTTP
    └── test_api.py
```

---

## 9. 打包

```toml
[project]
name = "funflix"
requires-python = ">=3.12"        # fundrive 2.0 的要求
dependencies = [
  "fastapi", "uvicorn[standard]", "sqlalchemy[asyncio]>=2.0",
  "alembic", "pydantic>=2", "pydantic-settings",
  "httpx", "anthropic", "typer", "fundrive>=2.0.85",
  "aiosqlite",
]

[project.optional-dependencies]
pg    = ["asyncpg"]
drives = ["fundrive[all]"]
zh    = ["opencc-python-reimplemented"]   # 繁简转换
dev   = ["pytest", "pytest-asyncio", "respx", "ruff", "mypy"]

[project.scripts]
funflix = "funflix.cli:app"
```

---

## 10. 实施顺序

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| M1 ✅ | config / db / models / alembic / `POST /raw` + `GET /raw/{id}` | 原始文本能进能出 |
| M2 ✅ | `linkscan` 正则 + `normalize` 归一 + 单测 | 纯函数层，无外部依赖，先测扎实 |
| M3 ✅ | LLM 抽取 + `extraction` 缓存 + 落库 pipeline | 端到端出结构化数据 |
| M4 ✅ | verify 抽象 + quark/alipan 两个匿名探针 + 限流 | 校验闭环 |
| M5 ✅ | worker claim/lease + 周期扫描 + `funflix worker` | 可靠性 |
| M6 🚧 | `/search` + SearchBackend + media 聚合 | 服务层与 CLI 已有；缺 HTTP 路由与 SqliteFtsBackend |
| M7 🚧 | 其余网盘探针、admin stats、CLI 批量导入 | 批量导入已有；缺其余探针与 admin 接口 |

---

## 11. 已知风险

1. **LLM 幻觉 URL** —— 已用 §4.2 的"原文回查"硬性拦截。这是必须做的，不是可选优化。
2. **全量 LLM 成本** —— 靠 `(raw_document_id, model, prompt_version)` 唯一索引做缓存，同文本不重复调用；`content_hash` 在入口再挡一层。建议加日额度上限，超额的文档留在 `pending`。
3. **`BackgroundTasks` 不持久** —— 已用库内状态机 + 租约 + 启动补偿把语义拉回 at-least-once；量级上来后换 arq/Celery 只需替换 claim 循环。
4. **网盘接口易变** —— 探针接口是逆向的私有 API，会随网盘改版失效。每个探针必须有独立契约测试和"连续失败告警"，避免把"探针挂了"误判成"链接全失效"。
5. **归一误合并** —— 同名不同年份的作品（翻拍）靠 `year` 区分；`year` 缺失时不合并到有 year 的记录，宁可留重复也不错合。
