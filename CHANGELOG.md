# CHANGELOG

本文件记录 funflix 的版本变更，按版本倒序排列。

## [未发布]

### 新增

- `src/funflix/compat.py`：按解释器版本择一导出 `enum.StrEnum` 与 `datetime.UTC`
  （都是 Python 3.11 才进标准库的名字），3.10 上的 `StrEnum` fallback 复刻标准库
  语义（成员是 `str`、`str()`/`format()` 取成员值、`auto()` 取小写成员名）。
- `tests/test_compat.py`：垫片行为、全仓库禁用 PEP 695 语法与 3.11+ 名字的直接导入、
  `pyproject.toml` 三处版本声明自洽。
- `tests/test_setup_script.py`：把 `scripts/setup.sh` 复制到临时目录、用只含必需命令的
  干净 PATH 驱动，覆盖 `bash -n`、用法、prod/dev 入口缺失、启动即退出、重复启动被拒、
  `status` 返回非 0、陈旧 PID 文件清理。

### 变更

- **Python 下限由 `>=3.12` 降到 `>=3.10`**（SPEC §3 的组织基线）：6 处 PEP 695 类型参数
  语法改写为 `typing.TypeVar`，`enum.StrEnum`/`datetime.UTC` 改从 `funflix.compat` 取；
  classifiers 补齐 3.10~3.13，Ruff `target-version` 改为 `py310`。测试在 3.10 与 3.13
  下都是全绿。
- `drives` extra（fundrive）补注释说明：fundrive 自身要求 Python ≥3.12，这个 extra 在
  3.10/3.11 上装不上；且 `src/` 下目前没有任何模块 import 它，它是给 `docs/DESIGN.md` §6
  规划的 `FundriveProbe` 预留的。
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
