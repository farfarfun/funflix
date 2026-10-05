"""API 出入参模型的统一导出口。

各模块按主题拆分（`raw` / `media` / `source` / `stats` / `common`），
这里再导出一份，调用方可以直接 `from funflix.schemas import XxxOut`。
"""

from funflix.schemas.raw import (
    BatchIngestResult,
    IngestResult,
    Page,
    RawDocumentBatchCreate,
    RawDocumentCreate,
    RawDocumentOut,
    RawDocumentSummary,
)

__all__ = [
    "BatchIngestResult",
    "IngestResult",
    "Page",
    "RawDocumentBatchCreate",
    "RawDocumentCreate",
    "RawDocumentOut",
    "RawDocumentSummary",
]
