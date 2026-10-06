# CHANGELOG

本文件记录 funflix 的版本变更，按版本倒序排列。

## [未发布]

### 新增

- **作品实体改成两层：`work`（一部剧）+ `media`（一季）**。搜「大主宰」原先返回
  448 行 media（同一部作品的噪声变体），现在是一条「大主宰（3 季 / 78 资源）」。
  根因是旧身份 `(norm_key, media_type, year)` 太脆 —— 同一部剧在不同分享里年份被
  识别成 0/2023/2025、类型被判成 anime/tv/unknown 就会裂成多行，生产库 92 万行
  media 对应 89 万个不同 norm_key。新身份：`work.norm_key` 唯一，
  `media` 是 `(work_id, season)` 唯一，`year`/`media_type` 退化成属性。
- `services/canon/`：归一流水线 —— `purge`（删页面文案/提取码/纯数字那类假作品，
  resource 行保留）、`rebuild`（规则重算 + 确定性建 Work）、`resolver`（LLM 裁决
  残局）、`merge`（应用裁决）。各阶段都支持 `--dry-run` / `--key` / `--limit`、
  按组提交可中断续跑。裁决结果落 `title_canon` 表，是完整审计记录，重跑 parse
  或新数据入库都不会把已经并好的东西重新拆开。
- `services/counters.refresh_work_counters()` 与 `refresh_counters_for_media()`：
  后者是**两级联动的默认入口**。搜索列表读的是 `work` 上的计数，季级刷完不往上
  滚一层的话，新入库的资源在结果里看不见而且不报任何错。作品归属必须在刷季
  **之前**问出来 —— 刷季会顺手物理删除零资源的季，行删掉就再也查不到它曾属于
  哪部作品。
- `MediaType` 新增 `book` / `comic` / `other` 三个**非影视**类型。采集源里混着大量
  小说、漫画、课程的分享，它们是真资源，所以保留但默认不进搜索结果
  （`services/search.VIDEO_MEDIA_TYPES`）；要查就显式传 `media_type`。
  `unknown` **留在**默认可见集合里：它是「还没判出类型」而不是「不是影视」，
  库里 40 多万部作品是这个值。
- `services/text/normalize`：`series_norm_key()` / `extract_season()` /
  `strip_season()` / `looks_like_junk_title()` / `strip_scrape_labels()`，以及噪声
  词表的大幅补齐（`更至03`、`每周自动更新`、`4K高码率版`、`《》「」`、URL 碎片、
  抓取字段标签等）。`extract_season` 只在高置信写法上给出季号，拿不准返回 `None`
  交给 LLM —— 真实数据上激进的正则抽季误报率不可接受。
- `tests/test_packaging.py`：`requires-python`、classifiers、Ruff `target-version`
  三处版本声明必须自洽。改一处漏两处不会有任何报错，而 Ruff 落后尤其阴险 ——
  它按旧版本的语法上限检查，新语法能用却被标成错误（本次就是这么发现的）。
- `tests/test_setup_script.py`：把 `scripts/setup.sh` 复制到临时目录、用只含必需命令的
  干净 PATH 驱动，覆盖 `bash -n`、用法、prod/dev 入口缺失、启动即退出、重复启动被拒、
  `status` 返回非 0、陈旧 PID 文件清理。
- **持续修复机制 `services/repair/` + `funflix repair` 三条子命令**（见
  `docs/DESIGN.md` §7.5）。解析规则永远加不完，每加一条就留下一批「按旧规则算
  出来、现在看是错的」数据，所以这不是一次性迁移脚本，而是可以反复跑的三条命令：
  `scan`（只读 media + 只写 `repair_task`，规则没变就零写入）、`apply`（破坏性、
  不可逆、带限额和爆炸半径闸门）、`requeue`（把规则版本过期的文档打回 parse 队列）。
  三条都默认 dry-run，`--apply` 才写库。要点：
  - 检测和应用**必须能分开调度**：误并不可逆（关联迁走、败者行删掉，没有信息
    能分回去），而检测只读且便宜。`scan` 把结论落成 `repair_task` 行，它同时是
    审计记录。`UNIQUE (kind, media_id) WHERE status = 'pending'` 这条部分唯一索引
    是幂等的支点 —— 每轮都会重新发现同一批行，没有它任务表会无限膨胀。索引挡不住
    **跨 kind** 的并存，所以诊断结论变了的旧任务要显式撤掉。
  - 目标值**复用 `canon/lookup.py::resolve_target`**，不另写规则。修复的终点必须和
    parse 现在会产出的结果一字不差，否则 repair 改成 A、parse 改回 B，无限循环。
  - 类型和年份**只补不改**（书刊是唯一例外）。反过来写的话，一轮 scan 就能把全库
    的 `media_type` 刷成 unknown —— 绝大多数行的类型来自正文 `类型:电影`，而只看
    标题会得到 `UNKNOWN`。
  - 判据取**库里存着的** `media.title`，不是再洗一遍的结果：`clean_title` 会把
    `作者:奏光 txt` 整段剥掉，洗完书刊信号就不存在了；`(2019)` 洗完年份也没了。
  - 爆炸半径闸门按 **media 总行数**算（不是队列长度 —— 拿队列当分母的话攒得越多
    越容易通过，正好反了）：删除 > 5% 或重挂 > 20% 就拒绝，要 `--force`。
    dry-run 时也检查，因为 dry-run 的作用就是让人提前看到这个拦截。
  - `collect.yml` 新增第四个 job `repair`，和 collect / parse / verify **完全并列、
    不设 `needs`**，刻意不传 `--force`，靠 `--limit` 慢慢刷。
- `repair_task` 表 + `raw_document.parse_rules_version` 列（迁移 `d6e7f8a9b0c1`）。
  版本戳是深层重解析的廉价预筛 —— 213 万份文档不可能每次改规则都全量重跑。
  `PARSE_RULES_VERSION` 手工 bump，**刻意不用源码哈希**：改个注释不该让 213 万份
  文档重排队。

### 变更

- **删掉 `scripts/canon-rollout.sh`（587 行）和 `tests/test_canon_rollout_script.py`。**
  它整套设计（COPY binary 备份、停 Action、九个人工闸门、迁移 B 收口）是为「原地改
  89 万行历史数据」服务的，而历史数据不需要保留 —— 只有采集（`raw_document`）和
  验证（`link_check`）的成果要留，其余都可以重建。而且实查发现它要原地迁移的历史
  Work 数据**根本不存在**：生产库 89 万行 media 里只有 80 行 `work_id` 非空，
  `title_canon` 80 行全是 pending。全量重建是唯一能产出可用 work 层的路径，也比
  原地改干净 —— 没有「迁移没覆盖到」的残留。持续修复的常态化手段改由
  `funflix repair` 承担。
- `services/maintenance.py::relink_checks()` 从逐行 `session.scalar` 改成攒批
  executemany：生产库有 848,416 个去重链接，原先就是 84 万次网络往返，全量重建
  卡在这一步要跑到天荒地老。现在一条流式查询读出每个链接的最新结论，按 5,000
  行一批走带 `bindparam` 的 UPDATE，往返降到 170 次左右。
- **搜索的主体从 `Media` 改成 `Work`**（`services/search.py`）：两个后端都返回
  `list[Work]`，`search_media`/`count_media` 改名为 `search_works`/`count_works`。
  资源筛选（`valid_only` / `provider`）因此要多穿一层，走完
  `work → media → media_resource → resource` 四张表。`SearchQuery` 新增
  `media_type` 与 `with_seasons`。
- `funflix search` 改成一部剧一条、季是子层的两级展示，新增 `--links` 控制每季
  列几条链接（默认 5）—— 热门剧某一季有 793 条分享，全列出来是几百屏噪声。
- `schemas/media.py` 新增 `WorkSummary` / `WorkDetail` / `SeasonSummary` /
  `SeasonDetail`，年份哨兵的处理抽成共享基类 `_YearBlanked`。
- `extract/runner.py`、`maintenance.py` 的计数刷新路径统一改走
  `refresh_counters_for_media`。
- `docs/DESIGN.md` §3.3 / §3.6 / §4.3 / §7.2 / §7.3 与 README 的流水线图、接口段
  按两层模型重写。
- SQLite 兜底库路径从 `~/.cache/farfarfun/funflix/funflix.db` 改为
  `~/.farfarfun/funflix/funflix.db`，与组织统一的 `~/.farfarfun/<包名>/` 约定对齐
  （funflix-api 的 `api/`、funflix-web 的 `web/` 也都挂在这个目录下）。放在
  `.cache` 下本来就不妥：那是「丢了可以重建」的语义，而这是数据库。
- README 里 `FUNFLIX_DATABASE_URL` 的默认值原先写的是 `sqlite+aiosqlite:///./funflix.db`
  （CWD 相对路径），与代码里的绝对路径兜底不符，改成真实值。
- **Python 下限统一为 `>=3.12`**（与 funflix-api 一致）。此前短暂降到 `>=3.10` 的那套
  3.10 兼容层整体撤掉：删掉 `src/funflix/compat.py`，`enum.StrEnum` / `datetime.UTC`
  改回从标准库直接导入，5 处 `typing.TypeVar` 回到 PEP 695 的 `def f[T]()` / `class C[T]`
  写法。Ruff 的 `target-version` 当时留在 `py310` 没跟着改回来，是这次唯一真的不一致的
  地方 —— 三处声明现在由 `tests/test_packaging.py` 守着。
- `drives` extra（fundrive）补注释说明：`src/` 下目前没有任何模块 import 它，
  它是给 `docs/DESIGN.md` §6 规划的 `FundriveProbe` 预留的。
- `docs/DESIGN.md` §9「打包」原文照抄的 toml 片段写着 `requires-python = ">=3.12"` 和
  fastapi/uvicorn/anthropic 等本仓库根本没有的依赖，改为说明真实约定并指向 `pyproject.toml`。

### 修复

- **`title_canon` 的键空间错配**（`services/extract/runner.py`）。parse 时的防回退
  查询用的是 `item.norm_key`（逐标题身份），而 `canon/resolver.py` 落裁决行用的是
  `series_norm_key`（系列身份），`canon/apply.py` 回头也按 `series_norm_key` 匹配。
  两个键空间对不上，于是花钱得出的裁决在新数据入库时经常被静默绕过 —— 正是这一层
  要防的事。统一到 `series_norm_key`。
  顺带修掉同一条路径上的一个撞主键：`resolve_target` 碰到「decided 但
  `work_norm_key` 为空」的坏裁决时会回落到 `_fallback`，而 `_fallback` 无条件把
  `needs_pending_row` 置真 —— 那个键在库里已经有行了，再插一行就撞主键、把整个
  SAVEPOINT 带崩。现在插 pending 行要同时满足 `canon is None`。
- **`_SCRAPE_CUT_RE` 补齐书刊字段名** `作者|译者|主播|演播|播音|出版社|字数|连载状态`
  （沿用「必须带冒号」的约束）。`全民攻防:我有签到系统 作者:奏光 txt` 这种小说分享
  在库里有 4,399 行，作者名整条粘在片名里。同时新增 `_BOOK_SIGNAL_RE` 把这个信号
  判成 `MediaType.BOOK`，并让它排在影视关键词**之前** —— 小说正文常顺带写「改编
  动画」「同名电视剧」，先跑影视词表会把 `作者:墨之所想 txt` 判成 anime。
  三个负向回顾（`原著作者:` / `词曲作者:` / `编曲作者:`）把「这是书」和「这改编自
  书 / 这是首歌」分开；刻意不收光杆 `txt`，因为 `_SHEET_MARKER_RE` 正把它当表格
  列名残渣剥，两边会打架。
  另外查清了 926 行「导演」残留的成因：670 行无冒号（`威力导演`、`今敏导演`、
  `导演评论`，冒号约束正好挡住）、19 行落在开头两字内（`:导演你有病` 是真片名，
  `match.start() < 2` 那道闸挡住）、237 行带冒号且全是本规则加上去之前解析的陈旧
  数据 —— 后者是 `funflix repair` 的活，不是词表的问题。
- `services/maintenance.py::relink_checks()` 判「最新一条校验」原先用
  `max(LinkCheck.id)`。id 是 uuid7，只到毫秒级单调 —— 同一毫秒内落库的两条校验，
  id 的大小由随机位决定，于是 `max(id)` 会随机挑一条。一条链接从 valid 变成
  invalid、两条记录又恰好同毫秒时，这个函数会把早已失效的链接恢复成 valid。
  改成按 `checked_at DESC, id DESC` 的 `ROW_NUMBER()` 窗口函数（PG 和 SQLite
  都支持；`DISTINCT ON` 只有 PG 有，单测跑在 SQLite 上）。
- **`db reset` 不再清掉登录账号。** `PRESERVED_TABLES` 原先只有
  `{source, alembic_version}`，`user` 落在清空清单里。生产库只有一个账号，
  而密码哈希是单向的 —— 清掉就只能重新 `funflix user create`，运维区当场锁死。
  登录账号和采集源同属**配置**，不是流水线产物；`db reset --help` 也只承诺
  「采集源配置保留」，顺手清账号属于意料之外的破坏。
- **迁移 `d6e7f8a9b0c1` 挪到收口迁移 `c5d6e7f8a9b0` 之前**，链变成
  `b1c2d3e4f5a6 → d6e7f8a9b0c1 → c5d6e7f8a9b0`。两条原先的顺序会死锁：
  `reset_pipeline_data` 的清表清单从 ORM 元数据推导、现在含 `repair_task`，
  PG 分支又是单条 `TRUNCATE ... CASCADE`，表不存在就整条失败 —— 于是 reset
  要求 `d6e7f8a9b0c1` 先上；而 `c5d6e7f8a9b0` 的 `work_id` 非空守卫要求先
  reset 清空 media。`d6e7f8a9b0c1` 纯加法、`repair_task` 刻意不建 media 外键，
  对收口迁移零依赖，挪到前面即可解环。两条迁移在任何环境都没应用过
  （生产库停在 `b1c2d3e4f5a6`），换序没有已部署的库受影响。
- **`httpx` 收紧到 `>=0.27,<1`，锁文件重新解析。** `uv.lock` 的
  `[options] prerelease-mode = "allow"` 让开区间的 `httpx>=0.27` 解析到了
  `1.0.dev6`，而那个开发版已经不再导出 `AsyncClient`。后果不止于测试：
  `services/collect/tencent_text.py` 和 `collect/concurrent_runner.py` 真的会调
  `httpx.AsyncClient(...)`，而 CI 的 collect job 用的是同一份锁文件。
  测试侧的表现是 respx 拦不住请求、整套 pytest 挂在真实网络上超时（本次两次
  600 秒被杀都是这个原因）。重新 `uv lock` 后 `prerelease-mode` 回到默认的
  `if-necessary-or-explicit`，httpx 落到 0.28.1，锁文件只动了 httpx 和 httpcore
  两项。
- `scripts/setup.sh`（SPEC §6.1）：
  - `start`/`run` 在拉起进程前校验环境入口——prod 用 `command -v funflix` 确认正式包
    已安装，缺失立即非 0 退出；dev 对应校验 `uv`。此前 prod 直接执行 `funflix worker`，
    入口不存在时后台进程立刻失败，脚本却照样打印「worker 已启动」。
  - `start` 后台拉起后等待 `FUNFLIX_START_WAIT`（默认 2）秒，确认进程确实存活才写
    PID 文件并报告成功；启动即退出会回显日志尾部 20 行并非 0 退出。
  - 重复 `start` 由 `exit 0` 改为非 0 退出，调用方才能发现「这次没真的启动」。
- 补齐 215 处公开函数/类/协议方法的中文 docstring（SPEC §7），覆盖 `base/`、`models/`、
  `schemas/`、`cli.py`、`services/`（collect/extract/verify/sync/text 及 ingest、
  maintenance、search、stats）与 `worker/`，`src/` 下已无遗漏。
- 清掉两处死代码（无行为变化）：`PgTrgmSearchBackend._keyword_clause` 收了个从不使用的
  `similarity` 形参；`_ParseProcessor` 把 `extractor_override` 存进实例属性后无人读取
  （每条用哪个抽取器由生产者写进 `extractor_kind` 决定）。
- README 的 `scripts/setup.sh` 一节补充 dev/prod 差异、启动确认与非 0 语义。
- 删除 `pyproject.toml` 里空的 `[tool.setuptools]`（构建后端是 hatchling，这段是死配置）。
- 文档与实现对齐：
  - README「快速开始」原来让用户跑 `funbuild install`，但 funbuild 既不是本包的
    依赖也不在 `dev` extra 里，照着做会 command not found；改为 `pip install funflix`
    + `funflix db upgrade`（原来写的 `alembic upgrade head` 要求在仓库目录里）。
    funbuild 作为维护者工具挪到 `docs/DEVELOPMENT.md` 并说明要单独安装。
  - `docs/DESIGN.md` §7「API 设计」描述的 `GET /search`、`check_status` 默认只返
    valid、`SqliteFtsBackend`、`/api/v1/admin/stats` + API Key 都不存在；改为按
    funflix-api 的真实路由重写（`GET /media`、`valid_only` 默认 false、
    两个搜索后端、`GET /api/v1/stats`），并标注 `quality`/`sort`/`reparse`/`recheck`
    仍未实现。
  - `docs/DESIGN.md` §1（主键其实是 UUIDv7 不是自增 BigInteger、抽取器有规则/表格/
    LLM 三类且走 OpenAI 兼容协议）、§3.0/§3.1/§3.5（补 `extra`、`source_id`、
    `next_parse_at`、`last_parsed_at`、`latency_ms` 等字段）、§6.1（`LinkProbe` 实际
    没有 `patterns`/`parse()`，并写明新增网盘要改的四处）、§8 目录结构（按 `src/`
    布局重写）、§10 里程碑一并改正。
  - `docs/TODO.md` §5.6 从「DESIGN.md 多处与代码矛盾」收敛成仅剩的代码侧问题：
    把 `patterns` 收回探针上。

## [1.0.5] - 2026-10-05

### 变更

- 仅版本号变更的重新发布，代码无改动。

## [1.0.4] - 2026-10-05

### 修复

- `.github/workflows/collect.yml` 三个 job 的 `uv sync --locked` 改回 `uv sync`：
  1.0.3 的发布提交把 `uv.lock` 从仓库里删掉了，`--locked` 会因为找不到锁文件直接失败。

## [1.0.3] - 2026-10-03

### 新增

- `scripts/setup.sh`：统一管理 worker 生命周期（`start`/`stop`/`restart`/`run`/`status`），
  运行时文件落在 `.run/`。

### 修复

- 以下为 farfarfun/todo-list#676 的审计修复：
  - `scripts/setup.sh` 参数顺序由 `<dev|prod> <action>` 改为 `<action> <dev|prod>`；
    `status` 改为不区分环境、不需要参数；重复启动检测改为区分「PID 文件存在但进程已
    退出或已被复用」（陈旧 PID，自动清理并提示）与「进程真的存活」两种情况。
  - `dispose_engine()` 释放引擎后清空 `get_engine`/`get_sessionmaker` 的 `lru_cache`，
    避免调用后下一次 `get_engine()` 返回已关闭的旧引擎。
  - `security.py` 的 `hash_password`/`verify_password`、`base/db.py` 的 `create_engine`/
    `get_engine`/`get_sessionmaker`/`dispose_engine` 补充中文 docstring。
  - README 顶层命令表被 `scripts/setup.sh` 用法说明从中截断，导致 `probes`/`extractors`/
    `search`/`doc`/`ingest` 几行脱离表格无法正常渲染；改为先完整列出命令表，再另起一段
    说明 `scripts/setup.sh`。
  - `.gitignore` 补充 `.run/`、`*.rar`、`.idea/`、`.vscode/`。
  - `.github/workflows/collect.yml` 三个 job 改用 `astral-sh/setup-uv` + `uv sync --locked`
    + `uv run`，不再绕开锁文件用 `pip install -e .`。

### 变更

- 发布提交同时删除了 `uv.lock`。该文件此后一直不在仓库里，`uv sync --locked` 因此在
  1.0.4 被改回 `uv sync`。

## [1.0.2] - 2026-09-21

### 新增

- 新增 `CHANGELOG.md`；README 补齐组织介绍区块。

### 修复

- 日志统一改为 `from farlog import getLogger`，移除裸用的 `logging` 模块与 CLI 里的
  `logging.basicConfig` 自定义 handler 配置。
- `services/collect/rss.py` 里解析 feed 描述 HTML 失败时不再静默 `pass`，改为记录带
  异常上下文的 warning 日志，再回退到原始文本。
- `typer` 锁定为 `>=0.12,<0.26`（而不是另行声明 `click` 依赖）：typer 0.26 起把 click
  整份 vendor 进私有模块，`cli.py` 用 click 类型做的 `isinstance` 内省会全部失效。

### 变更

- 接入 uv 管理依赖：补全依赖版本下限（新增 `farlog>=1.1.8`，`funsecret` 抬到 `>=1.4.95`），
  开发文档改为 `uv sync`/`uv run` 系列命令，不再以 `pip install -e` 作为开发环境依赖
  管理方式。该版本曾提交 `uv.lock`，但它在 1.0.3 的发布提交里被删除。

## [0.1.67] - 2026-09-16

### 修复

- 补充搜索、采集和校验公开 API 的中文 docstring。

### 变更

- 0.1.67 及更早版本的变更详情参见 git 历史与 PyPI 发布记录：
  <https://pypi.org/project/funflix/#history>。
