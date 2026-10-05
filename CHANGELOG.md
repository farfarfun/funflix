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
- `scripts/canon-rollout.sh`：canon 归一上线的一次性编排（见 `docs/DESIGN.md` §7.5），
  九个阶段固定顺序 `backup guard rehearse purge rebuild resolve merge finalize reopen`，
  每阶段完成写 `.run/canon-rollout/<stage>.done`，可 `--from <stage>` 续跑。
  要点：
  - `guard` 先停 `collect.yml` + `pipeline-watchdog.yml` 并记下原状态，`reopen` 只恢复
    原本是 active 的那些。采集与归一同时跑会边删边写，`rebuild` 刚建好的 Work 立刻
    又被新行绕开。
  - `backup` 用 `COPY ... WITH (FORMAT binary)` 走 psql 而不是 `pg_dump` —— 本机
    pg_dump 16 对 PG 18.4 直接 `aborting because of server version mismatch`。校验查
    `PGCOPY` 文件头**和尾部的 `ffff` 结束标记**：`gzip -t` 对「一个完整的 gz 里只装了
    半张表」是通不出错的，少了结束标记才看得出截断。
  - `resolve` 是唯一花钱的阶段，先 `--limit 3` 探一次、打印新落的 `title_canon` 行，
    人工确认后才放开全量；`rehearse` 之后还有一道人工闸。非 TTY 下没有 `--yes` 就拒绝继续。
  - 每个阶段前后打一组 16 项不变量快照。注意两项「计数不一致」的基线不是 0 而是
    64 / 80（迁移 A 时期建的那 80 个 Work 计数列从没刷过），当成「只看趋势」读，
    别拿它当通过条件。

### 变更

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
