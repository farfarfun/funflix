# 待优化清单

已知但尚未处理的问题。每条都记了**为什么值得做**和**从哪下手**，
免得几个月后回来只剩一句看不懂的结论。

排序按「不做会怎样」，不按工作量。

---

## P1 · 会悄悄写坏数据

这一档的共同点：**不报错**。日志干净、测试全绿、状态显示正常，
只有翻库才看得出来不对。

### 1.1 无法识别的链接把整条 URL 塞进 `share_id`

`services/text/linkscan.py:150` —— `identify_provider` 返回 None 时，
`provider, share_id = Provider.OTHER, url`，整条 URL 当成 share_id 落库。
而 `resource.share_id` 是 `String(255)`。

- SQLite 上悄悄写进去（不校验长度）
- PostgreSQL 上 `flush()` 直接抛 `DataError: value too long`

一条 300 字符的跳转/追踪链接就能触发。触发后 `parse_document` 的兜底
`except Exception` 会在**已经脏掉的 session 上**继续改 `doc`，
`_finish` 提交时抛 `PendingRollbackError`，文档被重捞 5 次后置 `failed`。

`Media.title` 同理：`segment.py` 的 `_blank_line_anchors` 会把一整行当标题，
没有长度上限，而 `media.title` 也是 `String(255)`。

**怎么做**：落库前截断（`url[:255]` / `title[:255]`），
或者给这两列换 `Text`。截断的话要在注释里写明 share_id 被截断后
`(provider, share_id)` 唯一性还成不成立。

### 1.2 `_upsert_media` 的类型放宽回退没有排序

`services/extract/runner.py:94-109` —— 精确匹配 `(norm_key, media_type, year)`
落空后，会按 `(norm_key, year)` 放宽再找一遍。那个 `select` **没有 ORDER BY**。

同一个 `norm_key` + `year` 下已经有 ANIME 和 TV 两行、而新来的一条
`media_type=UNKNOWN` 时，挂到哪一部取决于数据库返回顺序 ——
PostgreSQL 上可能因为一次 VACUUM 或计划变化就翻转，和 SQLite 也不一致。

**怎么做**：加 `.order_by(Media.id)`，并把偏好规则写进注释
（比如「优先挂资源最多的那部」还是「优先最早创建的」）。

### 1.3 `parse_document` 的兜底没有先回滚

`services/extract/runner.py:313` 的 `except Exception` 直接改 `doc` 的状态就返回，
**没有 `session.rollback()`**。

如果 `_persist` 是跑到一半才抛的（比如第 3 个 item 出错），
前两个 item 已经写进 session 的 media/resource/tag 会跟着
`parse_status=PENDING` 一起被 `run_parse_batch` 的 `_finish` 提交。
重试时 `_upsert_resource` 在已存在的行上再跑一遍，
`seen_count` 被重复累加 —— 而它是热度信号，会影响排序。

**怎么做**：`except` 里先 rollback，再用一个干净 session 更新状态；
或者把状态更新挪到 `run_parse_batch` 里做。

### 1.4 生产库里有 610 条列名损坏的文档

`d4ce8b2` 修好了采集器（补历史现在会先取列定义），但**已经落库的那批修不了** ——
正文里只有 `fn99gF：https://...` 这种原始字段 ID，没有任何标题可抽。

现状：其中 605 条还是 `pending`，每次 LLM 解析都会白烧一次调用。

**怎么做**：加一条正规命令（`funflix db skip-unparseable`）把它们标成 `skipped`，
判据是正文匹配 `^f[A-Za-z0-9]{5}：` 且不含中文列名。要可逆（改回 pending 即可重跑）。
> 我试过直接批量 UPDATE 生产库，被安全检查拦下了 —— 走正规命令这条路更合适，
> 也顺便有了测试。

---

## P2 · 风控来了没有刹车

### 2.1 完全没有熔断（DESIGN §6.3）

全仓 `grep 熔断|breaker|circuit` 零命中。

探针认得限流码（`quark.py` 的 `_RATE_LIMITED_CODES = {40001, 41013, 429}`），
但这个信号**只作用于当前这一条资源** —— `verify/runner.py` 给它排一次退避就结束了，
同批剩下的 19 条继续硬打同一个已经在限流的接口，每条各自退避、下一轮又一起回来。

更隐蔽的是：风控返回的常常不是 JSON 而是验证码页，那条路径归 `ERROR`
（`verify/base.py`），连 `RATE_LIMITED` 都不算。看统计只会看到「error 变多」，
没有任何东西说得出「这个网盘现在整体不通」。

**怎么做**：`services/verify/ratelimit.py`（DESIGN §8 早就给它留了文件名，
逻辑目前挤在 `runner.py` 里）加一个 per-provider 的 `open_until` 时间戳，
`check_resource` 在 `limiter.acquire` 之前查一次，命中就直接返回 RATE_LIMITED
不发请求；`worker/tasks.py` 的循环里顺手跳过同 provider 的剩余行。

### 2.2 限流器是进程内的固定间隔隔离器，不是令牌桶

`verify/runner.py` 的 `RateLimiter` 只算 `interval = 1/rate`，
比较 `now - last[provider]`，不够就 sleep。没有容量、不累积、不支持突发。

真正的问题是它是**进程内状态**。跑 2 个 `funflix worker`，
对同一个网盘的实际 QPS 就是 2 × 配置值 —— 租约防的是「同一条任务被重复处理」，
防不了「不同任务同时打同一个网盘」。配置里那个 1.0 在多进程下是一句空话。

**怎么做**：跨进程要落库一张 quota 表或接 Redis。
最省事的中间态是把配置值除以 worker 数并在 README 里写明。

### 2.3 请求间隔没有抖动

`RateLimiter` 的间隔是严格恒定的，`base/backoff.py` 的退避曲线也是纯确定性的。

严格等间隔是最容易被行为风控识别的模式之一；无抖动的退避还会让同一批
被限流的资源在 `next_check_at` 上聚成一簇，下一轮同时回来（惊群），
正好在网盘刚恢复时再撞一次。

**怎么做**：`acquire` 的 sleep 时长和 `backoff` 的返回值各加 ±20% 随机。

### 2.4 顺序枚举 `/media/{id}` 依然能抓走全库链接

issue #2 指出 `/resources/{id}` 没上锁、可以顺序枚举绕过列表接口的 AdminDep ——
已修（`70dffcd` 之后）。但**只修那一处会给虚假的安全感**：

`/media/{id}` 无凭据就返回同一份 `url` 与 `passcode`（`MediaDetail.resources`），
而 media id 同样是自增整数。`seq 1 100000` 打 `/media/{id}` 照样能把全库链接抓走。

这不是遗漏，是取舍：`/media/{id}` 就是产品接口本身，用户浏览一部剧拿到它的
链接是这个系统存在的理由，锁掉等于没有产品。所以 `/resources` 上那把锁防的是
**成批导出的便利**，不是保密 —— 这一点已经写进 `api/v1/resources.py` 的模块说明。

**真要挡枚举只能靠限流**，按 IP / 按 key 限制 `/media/{id}` 的调用速率。
目前没有任何限流。做之前先想清楚要防谁：链接本身来自公开频道，
真实损失是聚合结果被整表爬走，不是机密泄露。

### 2.5 User-Agent 是写死的单个串

`base/http.py` 的 `DEFAULT_UA` 是固定的 Chrome/120。
注释自己写着「太旧会被当成爬虫」，但没人跟版本。DESIGN §6.3 要求的是随机 UA 池。

---

## P3 · 采了但读不出来

### 3.1 `link_check` 只写不读

表建了、`ix_link_check_resource_time` 索引建了、`Resource.checks` 关系也定义了，
但全仓唯一读它的地方是 `services/stats.py` 的一个总行数。

而 `models/check.py` 和 `verify/runner.py` 两处注释都写明这张表存在的理由是回答
「这条链接什么时候挂的」和「某网盘最近整体失效率是不是异常」——
**后者正是区分「探针挂了」和「链接真失效了」的唯一信号**（DESIGN §11.4 列为必须）。

探针改版导致全库误判时，运维手上只有一个总行数。那个索引目前是纯成本。

**怎么做**：`schemas/media.py` 加 `LinkCheckOut` + `ResourceDetail`，
`GET /resources/{id}` 带最近 10 条（DESIGN §7.2 本来就这么要求的）。

### 3.2 统计缺「各网盘失效率」和「LLM token 消耗」

DESIGN §7.4 承诺三样，只兑现了第一样：

- 各状态计数 ✅
- **各网盘失效率 ❌** —— `resource_by_provider` 和 `resource_by_check` 是两个
  独立的一维分组，交叉不出来。改成 `group_by(provider, check_status)` 即可，
  同一次扫描不加成本。
- **LLM token 消耗 ❌** —— `Extraction.input_tokens/output_tokens` 一直在写，
  从没 SUM 过。DESIGN §11.2 把「日额度上限」列为成本控制手段，
  而现在连「已经花了多少」都查不到。

### 3.3 `ParseReport` 的两个字段算了没人看

`links_created` / `tags_linked` 在 `extract/runner.py` 里被认真维护，
但 `cli.py` 的打印和 `worker/tasks.py` 都不取。全仓无消费者。

---

## P4 · CLI 与 worker 已经分叉

### 4.1 `funflix parse` 看不见崩溃遗留的任务（已实测）

```
造一条 running + 租约已过期的文档：
  funflix parse   -> 没有待解析的文档     ← 撒谎
  funflix worker  -> 重捞 1 条，解析成功
```

`cli.py` 的 parse / verify 各自手抄了一份队列选取逻辑，且已经和
`worker/claim.py` 分叉：CLI 只认 `PENDING`，claim 还认「RUNNING 且租约过期」。
运维正是在队列堵住时才会去敲 CLI，而它会说一切正常。

CLI 那条路径还跳过了 `MAX_PARSE_ATTEMPTS` 保护，`--doc-id` 打在毒文档上会一直循环。

**怎么做**：两条命令都改走 `worker/tasks.py` 的 `run_*_batch`
（那边有测试），或者至少共用 `claim.py` 里的候选条件。

### 4.2 `cli.py` 零测试

1058 行，`grep funflix.cli` 在 src 和 tests 里零命中，没有 `CliRunner`。

`db reset` / `db retag` / `db requeue` 已经提到 `services/maintenance.py` 并有测试了，
那是其余部分该照抄的样板。还留在 CLI 里的实质逻辑：

- `ingest` 的文件格式解析（`.jsonl` 逐行 + `.txt` 按分隔符切，坏行 warn-and-skip）——
  这是唯一的批量导入入口，出错是静默的
- `_require_source` / `_toggle_source` / `_alembic_config`

而且 CLI 里的 import 全是函数内延迟导入，改服务层函数签名不会让测试变红，
只在敲那条命令时才炸。

---

## P5 · 清理与文档

### 5.1 确认死掉的成员（约 15 行，删掉不会坏任何东西）

grep 过 src 和 tests，五个都零引用：

| 位置 | 名字 | 备注 |
| --- | --- | --- |
| `models/media.py` | `year_or_none` | |
| `services/text/linkscan.py` | `ScannedLink.key` | 文档说它是「全局去重锚点」，但没人用 |
| `services/text/linkscan.py` | `ScannedLink.raw_url` | 只写不读 |
| `services/extract/base.py` | `ExtractionOutcome.attributed_count` | `SegmentedText` 上的同名属性是活的，别删错 |
| `services/text/normalize.py` | `extract_category()` | 只被门面 re-export，传递性死代码 |

### 5.2 七个门面 `__init__.py` 全是装饰性的，四个已经漂移

`funflix.services` / `.collect` / `.verify` / `.extract` / `.extract.llm` /
`.text` / `funflix.schemas` —— 约 185 行 re-export，**没有任何代码从它们导入**
（唯一一处是测试导入子模块，不是 `__all__` 里的名字）。

已经漂移的：`verify/__init__.py` 没有 `UCProbe`（照它读会以为 UC 没探针）、
`collect/__init__.py` 没有两个腾讯采集器、`services/__init__.py` 只有 `ingest`。

它们还会让 `import funflix.services.extract` 连带加载 httpx 和 LLM 客户端。

**怎么做**：要么删掉只留 docstring，要么加一条测试断言 `__all__` 与包内公开面一致。
不要留着「靠人记得同步」的状态 —— 现在的事实是 4/7 都没人同步。

### 5.3 标题标记词表分散在三处

- `normalize.py` 的 `_TITLE_MARKERS`（13 项，元组）
- `segment.py` 的 `_STRONG_ANCHOR_RE`（同样 13 项，硬编码成正则分支）
- `sheet.py` 的 `_TITLE_LABELS`（8 项，frozenset，少了 `影片名称`/`剧集名`/`番名`/`title`/`name`）

> 注：我实测过 `番名`/`title` 这些列名在表格里**并不会**认错 ——
> `find_title` 的兜底启发式接住了。所以这是冗余，不是当前的 bug。
> 但加一个新标记词要在三种写法里各改一遍，只有前两处是机械对应的。

**怎么做**：`normalize.py` 留唯一一份元组，另外两处从它派生。

### 5.4 `tencent_sheet.py` 有一份逐字重复的消息构造

`_to_messages` 方法和 `fetch` 里的内联副本一模一样（连注释都一样），
只有源字典变量名不同。`message_id` 的拼法和 docs.qq.com 的 URL 模板
是这个采集器的**去重契约** —— 改一处不改另一处，
补历史采到的行和追新采到的行会变成两条不同的 raw_document。

`fetch` 里那 11 行换成 `self._to_messages(doc_id, sheet_id, columns, collected)` 即可。

### 5.5 `normalize.py` 54% 是词表数据

569 行里约 308 行是纯数据（`_QUALITY_TOKENS`、`_GENRE_WORDS` 等）。
这些手工维护的中文发布圈词表是这个文件里**改得最频繁**的部分，
而它们的 diff 和逻辑改动长得一模一样，review 时只能靠扫。

拆成 `normalize/vocab.py`（数据）+ 逻辑，剩下约 260 行逻辑一眼能看完，
5.3 那份共享词表也就有了明显的落脚点。保留 `normalize.py` 作 re-export 壳，
调用方不用动。

### 5.6 DESIGN.md 多处与代码矛盾

- **§6.1 描述了一个不存在的扩展点**（唯一不算 cosmetic 的一条）：
  写着 `LinkProbe` 有 `patterns` 和 `parse()`，`linkscan` 用所有 probe 的
  patterns 并集扫原文，「新增一个网盘 = 新增一个文件」。
  实际 `LinkProbe` 只有 `name/provider/needs_auth/check()`，URL 正则独立住在
  `linkscan.py` 的 `_PROVIDER_PATTERNS` 里。**新增一个网盘要改四处**，
  而漏掉 linkscan 那处不会有任何报错 —— 链接会被静默记成 `Provider.OTHER` 永不校验。
  要么改文档，要么把 patterns 收回探针上。
- §8 目录结构基本全错（`src/` 布局、`base/`、`services/extract/` 而非 `parse/`、
  `repository/` 从未存在、`ratelimit.py` 不存在、漏了 `collect/`、`counters.py`、
  `maintenance.py`、`models/source.py`、`models/tag.py` 等）
- §10 里程碑表已过时
- §3.1 缺 `next_parse_at`、§3.5 缺 `latency_ms`、§3.0 缺 `extra`
- §7.2 的 `GET /search` 实际是 `GET /api/v1/media`，且少了
  `provider` / `quality` / `sort(latest|hot)` 三个筛选维度，
  `check_status` 默认值与文档相反（文档说默认只返 valid，实际默认全返），
  列表项也没有 inline 的 `resources[]`（客户端拿到 20 条得再发 20 次详情请求）。
  `Resource.seen_count` 采了但没有任何排序用到它，`sort=hot` 无从实现。
- §7.1 的 `POST /raw/{id}/reparse` 和 `POST /resources/{id}/recheck` 都不存在
  （`base/config.py` 的注释还在给这两个端点做承诺）。CLI 有等价能力，
  缺的是 HTTP 面。补的时候注意：一旦有了 recheck，人工触发就会和 worker
  并发打同一行，那时才真的需要单飞（现在靠 `UNIQUE(provider, share_id)` +
  租约结构性地回避掉了）。

---

## P6 · 想做但不急

### 6.1 `SqliteFtsBackend` 一直没实现

DESIGN §7.3 规划了三个后端，只有两个存在，SQLite 一律回落 `LIKE %x%` 全表扫描 ——
正是 `search.py` 开头花两段论证「必须换掉」的那条路径。

**当时决定不做的理由**（值得保留）：库还小，`LIKE` 到上万行才有感觉；
而中文用 FTS5 得上 trigram 分词器，它同样有「少于 3 字符查不了」的限制，
「误杀」这种两字关键词照样落不到索引。加一张虚拟表 + 一组触发器 +
一个只在 SQLite 生效的迁移，换来覆盖不全的收益。

数据量真起来时，**换 PostgreSQL 比补 FTS5 划算** —— pg_trgm 那条路已经通了、
有实测数据（5 万行 3 字关键词 63.9ms → 0.235ms）、有防退化的执行计划测试守着。

### 6.2 `extract-diff`：拿规则当 LLM 的回归网

`rule` 抽取器的存在理由之一就是「拿它和 LLM 的产出做 diff，
看 LLM 到底赢在哪、有没有变笨」（它自己的文档这么写的），但没有任何地方真的跑过。

在真实语料上手工跑过一次 6 条，4 条一致、2 条分歧，**两次都是 LLM 对规则错**
（规则把「更144部」「4K有更新」当成剧名）。其中一条已经反过来修好了规则
（`86782b1` 补了「更N部」）—— 这正说明这个对比有价值。

**怎么做**：`funflix extract-diff --limit N`，同一批文本两个抽取器各跑一遍，
报告标题 / 年份 / 季 / 链接归属的分歧。既能量化 LLM 的收益，
也能在换模型或改 prompt 后当回归网。

### 6.3 零宽字符会留在展示标题里

`clean_title("剧​名")`（中间是零宽空格）返回 `'剧​名'`，
而 `norm_key` 会把它抹掉。所以合并是对的（两条会归到同一部），
但**先入库的那条如果带零宽字符，展示标题就一直带着**，前端渲染出来有隐形字符。

`clean_title` 里加一步剥控制字符即可。

---

## 已知但不打算改的

记在这里免得反复重新发现。

### 腾讯表格那个源天然无法归属

`DT0xZd3FMRHFKeXVT` 的行内容是 `文本 1：08月04日丨短剧更新19部` ——
**每行是一天的更新打包，本来就没有单部作品的标题**。
链接是真的、有价值，但归不到具体剧名上。这是数据形态，不是 bug。

### `SourceType` 的枚举值与模块名不一致

模块改名成了 `tencent_sheet.py` / `tencent_text.py`，
但枚举值仍是历史命名 `"tencent_docs"` / `"tencent_doc"`（只差一个 s）。
值落在 `source` 和 `raw_document` 两张表里，改了库里已有的行就再也匹配不上采集器，
那些源会静默停止采集。要两边一致得配一次数据迁移 —— 收益不抵风险。

### 本地开发环境是非 editable 安装

`python -m funflix.cli` 加载的是 site-packages 里的副本，不是 `src/`。
跑 `pip install -e .` 可解。测试不受影响 ——
`pyproject.toml` 的 `pythonpath = ["src"]` 保证了这点，
那行注释预见的正是这个情况。
