"""LLM 抽取子包：客户端、抽取器与 prompt 定义的对外导出入口。

真正的实现分散在同目录的 `client.py`（OpenAI 兼容协议调用）、
`extractor.py`（`LLMExtractor`，负责不信任模型输出的结构化校验）、
`prompts.py`（system/user prompt 与工具 schema）三个模块，这里只是
把它们常用的类/函数收拢到包级命名空间，方便 `from funflix.services.extract.llm import ...`。
"""

from funflix.services.extract.llm.client import (
    LLMCallError,
    LLMClient,
    LLMConfigError,
    LLMResult,
    OpenAICompatClient,
    build_default_client,
)
from funflix.services.extract.llm.extractor import LLMExtractor, format_link_lines, parse_payload
from funflix.services.extract.llm.prompts import PROMPT_VERSION

__all__ = [
    "PROMPT_VERSION",
    "LLMCallError",
    "LLMClient",
    "LLMConfigError",
    "LLMExtractor",
    "LLMResult",
    "OpenAICompatClient",
    "build_default_client",
    "format_link_lines",
    "parse_payload",
]
