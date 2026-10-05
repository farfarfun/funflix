# funflix 设计文档

> 采集网上分享的影视资源文本 → LLM 结构化抽取 → 剧名归一 → 网盘链接有效性校验 → 提供查询接口。

## 1. 技术选型（已确认）

| 维度 | 选择 | 说明 |
| --- | --- | --- |
| Web | FastAPI | 全异步，Pydantic v2；**不在本仓库**，见 funflix-api |
| ORM | SQLAlchemy 2.0（Declarative + `Mapped[]`） | 异步 session |
| DB | SQLite 起步，`DATABASE_URL` 可切 PostgreSQL | schema 只用两库共有类型 |
| 迁移 | Alembic（`render_as_batch=True`） | SQLite 的 ALTER 限制 |
| 抽取 | 规则 / 表格 / LLM 三类抽取器（`extractor_kind` 选） | LLM 走 OpenAI 兼容协议（`llm` extra），非 anthropic SDK |
| 网盘 | fundrive 2.0 + 自研 HTTP 探针 | 见 §6 |
| 后台 | 库内状态机 + 租约领取 + `asyncio` 常驻循环 | 见 §5 |
| 凭证 | funsecret（fundrive 原生配置方式） | 不入库不入 git |

### SQLite → PG 的兼容约束

- 用 `sa.JSON`，不用 `JSONB`；PG 上通过 `.with_variant(JSONB, "postgresql")` 自动升级。
- 所有 `DateTime(timezone=True)`，应用侧统一写 UTC-aware。
- 主键统一 **UUIDv7**（`sa.Uuid(as_uuid=True)`，客户端 `uuid7()` 生成）：毫秒时间戳
  前缀保证字典序等于生成顺序，且多机并发写入不会撞号 —— 这是本地库拉取/推送同步
  （`services/sync/`）的前提，自增整数做不到。非主键的大整数（`size_bytes`）才用 `BigInteger`。
- 不用 PG 独有的 `ARRAY` / 部分索引 / `ON CONFLICT ... WHERE`；去重靠普通唯一索引 + 应用层 upsert。
- 模糊搜索抽象成 `SearchBackend` 协议，按方言自动选实现，见 §7.3。PG 走 `pg_trgm`；
  SQLite 目前只有 `LIKE` 兜底，规划中的 FTS5 后端还没落地。

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

一个 Source 是一个可持续拉取的消息流（如一个 Telegram 频道或公开 RSS/Atom feed）。它持有**水位**，
每次采集只取水位之后的新消息，把正文写成 RawDocument 后即结束职责。

| 字段 | 说明 |
| --- | --- |
| `id` | |
| `source_type` | 复用 `SourceType`，采集器注册表按它分发 |
| `url` | 采集源地址，如 `https://t.me/s/<频道名>` 或公开 RSS/Atom feed |
| `identifier` | 规范化标识（Telegram 为频道名，RSS 为去掉 fragment 的 feed URL）。同一源的多种写法必须归一后再做唯一性判定 |
| `title` | 展示名，首次采集时自动回填 |
| `enabled` / `fetch_interval_seconds` / `max_pages_per_fetch` | 调度配置 |
| `cursor_message_id` | **主水位**：已采集到的最大消息 ID |
| `cursor_published_at` | 辅助水位，仅供展示与人工核对 |
| `last_fetched_at` / `last_success_at` / `next_fetch_at` / `lease_until` | 调度状态 |
| `consecutive_failures` / `last_error` | 健康度，用于退避与告警 |
| `total_collected` | 累计产出的新 RawDocument 数 |
| `extra` | JSON，各采集器自己的状态（如表格类源的 sheet 偏移），结构由采集器定义 |

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
| `id` | UUID PK | `uuid7()`，客户端生成、时间有序 |
| `content` | Text | 原始文本全文，不做任何加工 |
| `content_hash` | String(64) **UNIQUE** | `sha256(normalize_ws(content))`，入口去重 |
| `source_id` | UUID FK 可空 | 来自哪个 `source`（手工导入时为空） |
| `source_type` | Enum | `SourceType`：`telegram` / `tencent_docs` / `tencent_doc` / `kdocs` / `rss` / `forum` / `web` / `api` / `manual` 等 |
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
| `next_parse_at` | DateTime(tz) | 可空，重试退避到点时间（`base/backoff.py`）；没到点不领 |
| `last_parsed_at` | DateTime(tz) | 可空，最后一次解析完成时间 |

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

`id`(UUID), `resource_id` FK, `provider`, `share_id`, `url`, `checked_at`, `status`,
`http_code`, `probe`(String，用了哪个探针实现), `detail`(Text), `latency_ms`(Int，单次探测耗时)

`provider` / `share_id` / `url` 在这里冗余存一份：resource 行可能被清理或改写，
而失效率统计要能回答"当时打的是哪个地址"。

保留时序，用于回答"这条链接什么时候挂的""某网盘最近整体失效率"。可按保留期归档。

### 3.6 关系

`raw_document 1─n extraction`、`raw_document 1─n resource`、`media 1─n resource`、`resource 1─n link_check`。

---

## 4. 解析层

### 4.1 LLM 抽取

**凭证来源（已定）**：走 `funsecret`，不进环境变量也不进代码：

```python
from funsecret import read_secret

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

进程内 worker（`FUNFLIX_WORKER_ENABLED`）**默认关闭**：一是开着的话 `funflix server start`
会自己开始调 LLM、探网盘，一条真实花钱的副作用不该由"起个 API"隐式触发；
二是 uvicorn 多 worker 部署时每个进程都会起一份，租约虽能防重复处理，但白白多出几倍空转。
生产建议用独立的 `funflix worker` 进程。

重试：指数退避 `min(60s * 2^attempts, 6h)`（`base/backoff.py`，三条流水线共用），
解析 `attempts >= 5` 置终态 `failed`。校验不设终态 —— 探测很便宜，靠退避封顶即可。

---

## 6. 网盘校验层

### 6.1 抽象

```python
@dataclass(frozen=True, slots=True)
class LinkRef:
    provider: Provider
    share_id: str
    url: str
    passcode: str | None = None


@dataclass(slots=True)
class CheckOutcome:
    status: CheckStatus
    http_code: int | None = None
    detail: str | None = None
    title: str | None = None       # 网盘侧返回的资源名，可回填校正
    size_bytes: int | None = None
    sharer_id / sharer_name / sharer_avatar_url: str | None = None
    latency_ms: int | None = None

    @property
    def is_conclusive(self) -> bool: ...   # 只有 VALID/INVALID/NEED_PASSWORD 算结论


class LinkProbe(Protocol):
    name: str                      # 写进 link_check.probe，换实现后能区分历史数据
    provider: Provider
    needs_auth: bool

    async def check(self, ref: LinkRef) -> CheckOutcome: ...
```

**URL 识别不在探针上。** `LinkProbe` 只负责「这条链接现在还能不能用」；
「这是哪家网盘、share_id 是哪一段」由 `services/text/linkscan.py` 的
`_PROVIDER_PATTERNS` 单独维护。所以新增一个网盘实际要改四处：

1. `base/enums.py` 的 `Provider` 加枚举值（以及 `CHECKABLE_PROVIDERS`）；
2. `services/text/linkscan.py` 的 `_PROVIDER_PATTERNS` 加 URL 正则；
3. `services/verify/<provider>.py` 写探针（匿名 HTTP 的继承 `AnonymousHttpProbe`，
   只写 `endpoint` / `build_payload` / `classify`）；
4. `services/verify/registry.py` 的 `_REGISTRY` 注册。

**漏掉第 2 步不会有任何报错** —— 链接会被静默记成 `Provider.OTHER`，
从此不进校验队列。把 patterns 收回探针上能让这四处变回一处，
是个尚未做的重构，见 `docs/TODO.md` §5.6。

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

HTTP 面不在本仓库，由独立的 [funflix-api](https://github.com/farfarfun/funflix-api)
提供（它依赖本包，复用这里的模型、schema 与 `services/`）。本节描述的是**当前真实
实现**的接口形态；本仓库只提供 CLI（`funflix ...`）与 worker。

路由前缀由 funflix-api 的 `api_prefix` 配置决定，默认 `/api/v1`。除 `auth` 外
多数接口要求已登录（session cookie）。

### 7.1 写入

- `POST /raw` — 单条入库，body 为 `RawDocumentCreate`，返回 `{id, content_hash, duplicated}`。
  `duplicated=true` 时直接返回已有记录，不重复消耗 LLM。
- `POST /raw/bulk` — 批量入库，返回 `BatchIngestResult`。
- `POST /sources` — 登记采集源；`POST /sources/{id}/collect`、`POST /sources/{id}/parse`
  手动触发单个源的采集 / 解析；`POST /sources/{id}/reset-parse` 重置解析水位。
- `POST /resources/providers/{provider}/verify` — 按网盘批量重校验。

暂未实现：按单条重跑的 `POST /raw/{id}/reparse` 与 `POST /resources/{id}/recheck`。
CLI 有等价能力，缺的只是 HTTP 面；补的时候注意人工触发会和 worker 并发打同一行，
那时才真的需要单飞（现在靠 `UNIQUE(provider, share_id)` + 租约结构性地回避掉了）。

### 7.2 查询

- `GET /media` — 主查询接口（不是 `/search`）。
  参数：`keyword`（剧名关键词，留空按入库时间倒序）、`media_type`、`year`、
  `valid_only`（**默认 `false`，即默认返回全部状态**）、`provider`、`page`/`size`。
  返回 `Page[MediaSummary]`，**列表项不内联 `resources[]`**，要详情得再请求详情页。
- `GET /media/{id}` — 作品详情 + 资源 + 标签，关联对象一律预加载
  （异步会话下懒加载会在序列化时抛 `MissingGreenlet`）；资源最多返回 200 条，
  `resource_count` 仍是真实总数。
- `GET /resources` — 按 `provider` / `check_status` 翻页，按 `id` 倒序（不是
  `last_seen_at`，后者会被 ingest 改写导致翻页时行在页间来回移动）。
- `GET /resources/{id}`、`GET /raw`、`GET /raw/{id}`、`GET /sources`、`GET /sources/{id}`。

尚未支持的筛选 / 排序维度：`quality`、`sort=latest|hot`。`Resource.seen_count`
采了但没有任何排序用到它，`sort=hot` 目前无从实现。

### 7.3 搜索后端抽象

定义在本仓库 `src/funflix/services/search.py`：

```python
@runtime_checkable
class SearchBackend(Protocol):
    name: str

    async def search(self, session: AsyncSession, query: SearchQuery) -> list[Media]: ...
    async def count(self, session: AsyncSession, query: SearchQuery) -> int: ...
```

筛选条件收敛在 `SearchQuery` 这个 dataclass 里（`keyword` / `media_type` / `year` /
`valid_only` / `provider` / `limit` / `offset`），而不是散成一串位置参数。

实际只有两个实现，由 `get_backend()` 按数据库方言自动选择：

- `PgTrgmSearchBackend`：`pg_trgm` GIN 索引，关键词子句必须写成 `a % b`
  而不是 `similarity(a, b) > 阈值` —— 两者结果一样，只有前者走索引。
- `LikeSearchBackend`：兜底，`LIKE %q%`，小数据量够用。

规划中但**尚未实现**的 `SqliteFtsBackend`（`media_fts` FTS5 虚拟表 + 触发器同步）
见 `docs/TODO.md` P6；在它落地之前，SQLite 一律回落到全表扫描的 `LIKE`。

### 7.4 运维

- `GET /healthz`（无前缀，不需要登录）。
- `GET /api/v1/stats` — 流水线各状态计数（不是 `/api/v1/admin/stats`，也没有
  单独的 API Key header 保护，走和其他接口一样的 session 鉴权）。

LLM token 消耗与各网盘失效率目前不在这个接口里。

---

## 8. 目录结构

```
funflix/
├── pyproject.toml  README.md  CHANGELOG.md
├── docs/           DESIGN.md  TODO.md  DEVELOPMENT.md
├── alembic.ini  migrations/versions/
├── scripts/setup.sh              # worker 生命周期（SPEC §6.1）
├── src/funflix/
│   ├── compat.py                 # 3.11+ 名字（StrEnum / UTC）的版本垫片
│   ├── security.py               # 登录密码哈希
│   ├── cli.py                    # typer 入口 + 交互式菜单
│   ├── base/
│   │   ├── config.py             # pydantic-settings
│   │   ├── db.py                 # async engine / session
│   │   ├── enums.py              # Provider / CheckStatus / ParseStatus ...
│   │   ├── backoff.py            # 三条流水线共用的指数退避
│   │   ├── http.py               # 公共请求头
│   │   └── commit_batcher.py     # 按条数/时间节流提交
│   ├── models/                   # SQLAlchemy 2.0：raw/media/resource/check/
│   │   └──                       # extraction/source/tag/user/association/base
│   ├── schemas/                  # Pydantic I/O：common/media/raw/source/stats
│   ├── services/
│   │   ├── ingest.py             # 原始文本入库 + content_hash 去重
│   │   ├── counters.py  stats.py  maintenance.py
│   │   ├── search.py             # SearchQuery + Like/PgTrgm 两个后端
│   │   ├── collect/              # 采集器：telegram/rss/web/collection/kdocs/
│   │   │                         # tencent_sheet/tencent_text/yyets + registry
│   │   │                         # + runner / concurrent_runner / priority
│   │   ├── extract/              # 抽取：rule / sheet / llm（client/extractor/
│   │   │                         # prompts）+ registry + runner/concurrent_runner
│   │   ├── text/                 # 纯函数层：linkscan / normalize / segment
│   │   ├── verify/               # 探针：quark/uc/alipan/pan123/ctfile
│   │   │                         # + base（AnonymousHttpProbe）+ registry
│   │   │                         # + runner（限流）/ concurrent_runner
│   │   └── sync/                 # 跨库同步：runner / tables
│   └── worker/
│       ├── claim.py              # 租约领取
│       ├── scheduler.py          # asyncio 常驻扫描
│       └── tasks.py              # 单轮采集/解析/校验
└── tests/                        # 与上面一一对应，另有 test_compat.py
                                  # 与 test_setup_script.py 两个约定测试
```

注意几处与早期规划的差异：包是 `src/` 布局；没有独立的 `repository/` 层
（claim/lease 逻辑在 `worker/claim.py`）；解析目录叫 `extract/` 不是 `parse/`；
限流在 `verify/runner.py` 里而不是单独的 `ratelimit.py`；HTTP 层（`api/`）
整个在 funflix-api 仓库。
---

## 9. 打包

以 `pyproject.toml` 为准，本节只说明几个容易踩的约定：

- `requires-python = ">=3.10"`（SPEC §3 的组织下限）。源码里不用 PEP 695 类型参数
  语法（3.12），`enum.StrEnum` 与 `datetime.UTC`（都是 3.11 新增）统一从
  `funflix.compat` 取，该模块按解释器版本择一导出。
- HTTP 服务端（FastAPI/uvicorn）不在本仓库：funflix 只提供 `funflix` CLI
  （`[project.scripts]`），对外 HTTP 接口由 funflix-api 承载。
- LLM 抽取走 OpenAI 兼容协议（`llm` extra 装 `openai`），不直接依赖 anthropic SDK。
- `drives` extra（fundrive）目前没有任何模块 import，是给 §6 的 `FundriveProbe`
  预留的；fundrive 自身要求 Python ≥3.12，所以这个 extra 在 3.10/3.11 上装不上。
- `migrations/` 与 `alembic.ini` 通过 `[tool.hatch.build.targets.wheel.force-include]`
  打进 wheel，否则装完包跑 `funflix db upgrade` 会找不到 `script_location`。

---

## 10. 实施顺序

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| M1 ✅ | config / db / models / alembic / `POST /raw` + `GET /raw/{id}` | 原始文本能进能出 |
| M2 ✅ | `linkscan` 正则 + `normalize` 归一 + 单测 | 纯函数层，无外部依赖，先测扎实 |
| M3 ✅ | LLM 抽取 + `extraction` 缓存 + 落库 pipeline | 端到端出结构化数据 |
| M4 ✅ | verify 抽象 + quark/alipan 两个匿名探针 + 限流 | 校验闭环 |
| M5 ✅ | worker claim/lease + 周期扫描 + `funflix worker` | 可靠性 |
| M6 🚧 | 查询 + SearchBackend + media 聚合 | 服务层、CLI 与 funflix-api 的 `GET /media` 已有；缺 `SqliteFtsBackend`（SQLite 仍走 `LIKE` 全表扫描）与 `quality`/`sort` 筛选 |
| M7 🚧 | 其余网盘探针、运维接口、CLI 批量导入 | 批量导入与 `GET /api/v1/stats` 已有；缺百度/蓝奏/天翼等探针，缺按条重跑的 `reparse`/`recheck` HTTP 面 |

---

## 11. 已知风险

1. **LLM 幻觉 URL** —— 已用 §4.2 的"原文回查"硬性拦截。这是必须做的，不是可选优化。
2. **全量 LLM 成本** —— 靠 `(raw_document_id, model, prompt_version)` 唯一索引做缓存，同文本不重复调用；`content_hash` 在入口再挡一层。建议加日额度上限，超额的文档留在 `pending`。
3. **`BackgroundTasks` 不持久** —— 已用库内状态机 + 租约 + 启动补偿把语义拉回 at-least-once；量级上来后换 arq/Celery 只需替换 claim 循环。
4. **网盘接口易变** —— 探针接口是逆向的私有 API，会随网盘改版失效。每个探针必须有独立契约测试和"连续失败告警"，避免把"探针挂了"误判成"链接全失效"。
5. **归一误合并** —— 同名不同年份的作品（翻拍）靠 `year` 区分；`year` 缺失时不合并到有 year 的记录，宁可留重复也不错合。
