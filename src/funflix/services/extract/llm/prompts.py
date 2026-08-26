"""LLM 抽取的 prompt 与工具 schema。

**改动这里的任何内容都必须升 `PROMPT_VERSION`** ——
`extraction` 表按 `(raw_document_id, model, prompt_version)` 唯一做缓存，
不升版本会让旧结果被当成新 prompt 的产出，静默拿到过时数据。
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "v1"

TOOL_NAME = "submit_extraction"

SYSTEM_PROMPT = """\
你是影视资源分享文本的结构化抽取器。输入是一条从网络上采集的分享文案，\
你要从中抽出「有哪些影视作品」以及「每部作品对应哪些网盘链接」。

## 必须遵守

1. **一条文本里可能有多部作品**。逐一抽出，不要只抽第一部。
2. **一部作品可能有多个网盘链接**（夸克、阿里、百度各一份）。全部归到同一部作品下。
3. **链接只能通过序号引用**。用户消息里会给你一份已经扫描好的链接清单，\
每条带一个序号。你只能在 `link_indexes` 里填这些序号，**绝对不要自己写 URL**。\
清单里没有的链接就是不存在。
4. **标题要去噪**。剥掉画质（4K/1080p）、字幕（中字/内嵌）、集数（全40集）、\
压制组署名、表情符号、分类前缀（"电视剧："）。但**保留**「第X季」「第X部」以及\
片名里本身的数字续集标记——它们是作品身份的一部分。
5. **拿不准就填 null**，不要猜。年份、原名、集数信息缺失时留空即可。

## 目录帖

有些文案是「目录帖」/「合集帖」：标题是日期或"更新目录"之类，\
一个链接里打包了几十部作品。这类请把 `is_catalog` 设为 true，`items` 留空数组。\
不要把"8月25日短剧更新目录"这种当成一部作品的名字。

判断依据：标题是日期/期号/"目录"/"合集"/"打包"，或正文罗列了大量互不相关的片名\
而只给一个链接。

## 作品类型

`media_type` 取值：movie（电影）、tv（电视剧/短剧/网剧）、anime（动漫/国漫）、\
variety（综艺）、documentary（纪录片）、unknown（判断不了）。\
判断不了就填 unknown，不要默认填 movie。
"""

USER_TEMPLATE = """\
## 原文

{content}

## 已扫描到的链接清单

{links}

请调用 {tool} 提交抽取结果。link_indexes 只能填上面清单里出现过的序号。\
"""

NO_LINKS_PLACEHOLDER = "（原文中未扫描到任何网盘链接）"


#: 强制模型调用的工具。schema 里刻意不用 URL 字符串而用序号数组，
#: 让"幻觉出一个不存在的链接"在结构上就无法表达。
TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "提交从分享文案中抽取出的作品与链接归属结果",
        "parameters": {
            "type": "object",
            "properties": {
                "is_catalog": {
                    "type": "boolean",
                    "description": "是否为目录帖/合集帖（一个链接打包多部无关作品）",
                },
                "items": {
                    "type": "array",
                    "description": "抽取到的作品列表。一条文本可能有多部作品。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "去噪后的作品名，保留「第X季」等身份标记",
                            },
                            "original_title": {
                                "type": ["string", "null"],
                                "description": "外语原名，没有就填 null",
                            },
                            "year": {
                                "type": ["integer", "null"],
                                "description": "首播/上映年份，1900-2100，不确定填 null",
                            },
                            "media_type": {
                                "type": "string",
                                "enum": [
                                    "movie",
                                    "tv",
                                    "anime",
                                    "variety",
                                    "documentary",
                                    "unknown",
                                ],
                            },
                            "episode_info": {
                                "type": ["string", "null"],
                                "description": "集数描述，如「全40集」「S01E01-E12」，没有填 null",
                            },
                            "quality": {
                                "type": "string",
                                "enum": ["4k", "1080p", "720p", "sd", "unknown"],
                            },
                            "link_indexes": {
                                "type": "array",
                                "description": "该作品对应的链接序号，只能来自给定清单",
                                "items": {"type": "integer", "minimum": 0},
                            },
                        },
                        "required": ["title", "media_type", "quality", "link_indexes"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["is_catalog", "items"],
            "additionalProperties": False,
        },
    },
}


def build_user_message(content: str, link_lines: list[str]) -> str:
    links = "\n".join(link_lines) if link_lines else NO_LINKS_PLACEHOLDER
    return USER_TEMPLATE.format(content=content, links=links, tool=TOOL_NAME)
