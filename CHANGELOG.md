# CHANGELOG

本文件记录 funflix 的版本变更，按时间倒序排列。

## [未发布]

### 修复

- 日志统一改为 `from farlog import getLogger`，移除裸用的 `logging` 模块与 CLI 里的
  `logging.basicConfig` 自定义 handler 配置。
- `services/collect/rss.py` 里解析 feed 描述 HTML 失败时不再静默 `pass`，改为记录带异常上下文
  的 warning 日志，再回退到原始文本。
- 依赖下限补全：`funsecret>=1.4.95`；新增 `farlog>=1.1.8`。
- 开发文档改为 `uv sync`/`uv run` 系列命令，不再以 `pip install -e` 作为开发环境依赖管理方式。
- 重新生成并提交 `uv.lock`（此前在版本号升级提交中被意外删除）；`.github/workflows/collect.yml`
  三个 job 改用 `astral-sh/setup-uv` + `uv sync --locked` + `uv run`，不再绕开锁文件用
  `pip install -e .`。
- `scripts/setup.sh` 参数顺序由 `<dev|prod> <action>` 改为 `<action> <dev|prod>`，`status`
  改为不区分环境、不需要参数；重复启动检测改为区分「PID 文件存在但进程已退出/被复用」
  （陈旧 PID，自动清理并提示）与「进程真的存活」两种情况，不再把前者直接当后者处理。
- `.gitignore` 补充 `.run/`、`*.rar`、`.idea/`、`.vscode/`。
- `security.py` 的 `hash_password`/`verify_password`、`base/db.py` 的 `create_engine`/
  `get_engine`/`get_sessionmaker`/`dispose_engine` 补充中文 docstring。
- `dispose_engine()` 释放引擎后清空 `get_engine`/`get_sessionmaker` 的 `lru_cache`，
  避免调用后下一次 `get_engine()` 返回已关闭的旧引擎。
- README 顶层命令表被 `scripts/setup.sh` 用法说明从中截断，导致 `probes`/`extractors`/
  `search`/`doc`/`ingest` 几行脱离表格、无法正常渲染；调整为先完整列出命令表，
  再另起一段说明 `scripts/setup.sh`。

### 变更

- 提交 `uv.lock` 以保证可复现构建。

## [0.1.67]

### 新增

- 增加 `scripts/setup.sh`，统一管理 worker 生命周期。

### 修复

- 补充搜索、采集和校验公开 API 的中文 docstring。

### 变更

- 历史版本变更详情参见 GitHub Releases：<https://github.com/farfarfun/funflix/releases>。

### 废弃

- 无。
