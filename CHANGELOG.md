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
