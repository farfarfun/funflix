"""抽取环节：把原始文本变成「作品 + 链接」。

实现之间可互换，落库与状态机（runner）完全复用：

- `rule`：确定性规则，免费、离线、可作降级路径与对照基准
- `llm`：调用大模型，质量更高，凭证走 nltsecret
"""

from funflix.services.extract.base import ExtractedItem, ExtractionOutcome, Extractor
from funflix.services.extract.registry import get_extractor, supported_extractors
from funflix.services.extract.rule import RuleExtractor
from funflix.services.extract.runner import ParseReport, parse_document
from funflix.services.extract.sheet import SheetExtractor

__all__ = [
    "ExtractedItem",
    "ExtractionOutcome",
    "Extractor",
    "ParseReport",
    "RuleExtractor",
    "SheetExtractor",
    "get_extractor",
    "parse_document",
    "supported_extractors",
]
