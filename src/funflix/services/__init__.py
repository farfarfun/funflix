"""业务服务层，按流水线环节分目录。

每个环节一个基类 + 多个可互换实现，新增实现不改调用方：

- `collect/`  采集：telegram（已实现）、csv/rss（待扩展）
- `extract/`  抽取：rule（规则）、llm（大模型）
- `text/`     文本原语：链接扫描、分段、剧名归一（跨环节共用）
- `verify/`   校验：夸克、阿里云盘（M4）
"""

from funflix.services.ingest import (
    IngestOutcome,
    content_hash,
    ingest_document,
    ingest_many,
    normalize_for_hash,
)

__all__ = [
    "IngestOutcome",
    "content_hash",
    "ingest_document",
    "ingest_many",
    "normalize_for_hash",
]
