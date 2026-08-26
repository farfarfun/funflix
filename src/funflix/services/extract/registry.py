"""抽取器注册表。新增一种抽取方式 = 新增一个实现并在此注册。"""

from __future__ import annotations

from collections.abc import Callable

from funflix.base.enums import SourceType
from funflix.services.extract.base import Extractor
from funflix.services.extract.rule import RuleExtractor
from funflix.services.extract.sheet import SheetExtractor

#: 抽取器工厂。LLM 抽取器延迟构造 —— 它在 __init__ 里就要读凭证，
#: 提前构造会让"只用规则抽取"的场景也被迫依赖 nltsecret 配置。
_REGISTRY: dict[str, Callable[[], Extractor]] = {
    "rule": RuleExtractor,
    "sheet": SheetExtractor,
}


def _make_llm_extractor() -> Extractor:
    from funflix.services.extract.llm.extractor import LLMExtractor

    return LLMExtractor()


_REGISTRY["llm"] = _make_llm_extractor


def get_extractor(kind: str) -> Extractor:
    """按名字构造抽取器。未知名字直接报错并列出可选项。"""
    factory = _REGISTRY.get(kind)
    if factory is None:
        raise ValueError(f"未知的抽取器 {kind!r}，可选：{sorted(_REGISTRY)}")
    return factory()


def supported_extractors() -> list[str]:
    return sorted(_REGISTRY)


#: 来源类型 → 默认抽取器。
#: 表格源的每行本身就是结构化的，用 sheet 按列映射；
#: 自由文本源没有列可言，只能靠规则分段或 LLM。
#: 用错抽取器不会报错，只会静默地大批归属失败 —— 所以默认值必须按源类型分开。
_DEFAULT_BY_SOURCE: dict[SourceType, str] = {
    SourceType.TENCENT_DOCS: "sheet",
}

#: 自由文本源的默认抽取器
DEFAULT_EXTRACTOR = "rule"


def default_extractor_for(source_type: SourceType | None) -> str:
    if source_type is None:
        return DEFAULT_EXTRACTOR
    return _DEFAULT_BY_SOURCE.get(source_type, DEFAULT_EXTRACTOR)
