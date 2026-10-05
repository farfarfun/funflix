"""归一裁决的 prompt 与工具 schema。

**改这里的任何内容都要升 `CANON_PROMPT_VERSION`**（在 `models/canon.py`）——
`title_canon` 按 `norm_key` 缓存裁决，不升版本就没法区分"这条是旧 prompt 判的"。

和 `extract/llm/prompts.py` 是两套独立的东西，不要混：那一套回答"这段文案里
有哪些作品和链接"，这一套回答"这一堆已经入库的脏标题里，哪些是同一部作品"。
"""

from __future__ import annotations

from typing import Any

TOOL_NAME = "submit_canon_decisions"

#: 这个 prompt 最重要的任务是**不要并**，不是并。
#:
#: 误并不可逆：`天命大主宰` 被并进 `大主宰` 之后，两部作品的资源混在一行上，
#: 没有任何信息能把它们再分开。漏并是可逆的：下次调整 prompt 重跑就好，
#: 代价只有 token。所以整段规则都往"拿不准就各自独立"偏。
SYSTEM_PROMPT = """\
你是影视资源库的作品归一裁决器。输入是一组**已经机械清洗过**的标题键，\
它们因为字面相似被放进了同一个候选块。你要判断：这些键里，哪些指向同一部作品。

## 最重要的一条

**拿不准就判成独立作品**。把两部不同的作品错并成一部是**不可逆**的损坏 ——\
资源会永久混在一起；而漏并只是暂时的，下次还能再并。\
所以除非你确信是同一部，否则给不同的 `work_title`。

典型的「看着像、其实不是」：

- `大主宰` / `天命大主宰` / `诛天大主宰` / `北灵少年志之大主宰` / `深空彼岸大主宰4`\
—— 五部**完全不同**的作品，只是共用了「大主宰」三个字。必须五个不同的 `work_title`。
- `误杀` / `误杀2` —— 续集是独立作品，不是同一部的两季。
- `大主宰 我荒古圣体当为天帝 作者:墨之所想` —— 这是**小说**，不是那部动漫。\
`media_type` 填 `book`，`work_title` 用小说名。

## 什么才算同一部

同一部作品的不同**写法**才能并，典型是：画质/片源后缀（`4K高码`、`蓝光原盘`）、\
更新状态（`更新至87集`）、季的不同叫法（`第2季` / `年番2` / `S02` / `II`）、\
中外原名并列（`美国狙击手 American Sniper`）、重复采集（`大主宰 大主宰2 大主宰`）、\
压制组或频道署名。这些都只是同一部作品的噪声变体。

## 季

`season` 问的是**这个 key 本身是否锁定了某一季**，不是"这部作品有几季"：

- key 里带季号就填那个季号。`大主宰2` → 2；`斗罗大陆ii` → 2。\
机械清洗没认出来的季号写法，就是这一条要捞回来的东西。
- key 是**整部作品的通名**、底下混着好几季时，填 **null**。\
比如 `大主宰` 这个 key 覆盖了第 1 季和第 2 季的一堆分享，\
每条分享的季号另有规则逐条判定 —— 你在这里填任何数字都会把它们全压成同一季。
- 电影、确定只有一季的剧、综艺，填 0。
- 其余拿不准的，填 null。填错季号会让两季的资源混在一起，和误并一样难修。

注意：`大主宰 更新至S01E87` 里的 `S01` 是第 1 季，`E87` 是集数，不是季号。

## 不是作品

`is_junk` 为 true 的情况：网盘按钮文案（`夸克`、`查看资源`、`磁力下载`）、\
表格列名、纯数字、提取码、分享 ID、整页抓取残渣（`描述大主宰导演马建平编剧…`）、\
网盘客户端安装包。规则层已经拦掉了明显的，留给你的是需要读懂语义才看得出来的。

`is_junk=true` 时 `work_title` 填 null，其余字段随意。

## 类型

`media_type`：movie（电影）、tv（电视剧/短剧/网剧）、anime（动漫/国漫）、\
variety（综艺）、documentary（纪录片）、book（小说/电子书）、comic（漫画）、\
other（课程/软件/其它非影视）、unknown（判断不了）。判断不了就填 unknown，不要猜。

## 输出要求

1. **输入的每一个 key 都必须出现在输出里**，一个不能少，一个不能多。
2. `key` 字段必须**原样照抄**输入里的 key，不要清洗、不要改写。
3. 同一部作品的几个 key 要给**字面完全相同**的 `work_title`。
4. `work_title` 用最干净、最通用的那个片名（`大主宰`，不是 `大主宰 年番2 4K高码`），\
且**不要**把季号写进去。
5. `confidence` 填 0-1，表示你对这条裁决的确信程度。
"""

USER_TEMPLATE = """\
## 候选块

下面是同一个候选块里的标题键。每行格式：

    <key>  （出现 N 行 / M 条资源）  示例原始标题：…

{entries}

请调用 {tool} 提交裁决，输入的 {count} 个 key 每一个都要有一条对应的结果。\
"""

_MEDIA_TYPES = [
    "movie",
    "tv",
    "anime",
    "variety",
    "documentary",
    "book",
    "comic",
    "other",
    "unknown",
]

#: 刻意**没有** `work_norm_key` 字段 —— 它由本地 `series_norm_key(work_title)`
#: 算出来，不问模型。两个 key 只要被判成字面相同的 `work_title`，归一键就必然
#: 相同；让模型自己编一个键，反而会出现"标题一样但键不一样"的自相矛盾结果。
TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "提交候选块内每个标题键的归一裁决",
        "parameters": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "description": "每个输入 key 一条，不能少也不能多",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "原样照抄的输入 key",
                            },
                            "work_title": {
                                "type": ["string", "null"],
                                "description": "规范作品名，不含季号；is_junk 时填 null",
                            },
                            "season": {
                                "type": ["integer", "null"],
                                "description": "季号；无季概念填 0；拿不准填 null",
                                "minimum": 0,
                                "maximum": 99,
                            },
                            "media_type": {"type": "string", "enum": _MEDIA_TYPES},
                            "year": {
                                "type": ["integer", "null"],
                                "description": "首播/上映年份，1900-2100，不确定填 null",
                            },
                            "is_junk": {
                                "type": "boolean",
                                "description": "这个键根本不是一部作品",
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                        },
                        "required": ["key", "work_title", "media_type", "is_junk", "confidence"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["decisions"],
            "additionalProperties": False,
        },
    },
}


def format_entry(key: str, rows: int, resources: int, sample: str) -> str:
    return f"    {key}  （出现 {rows} 行 / {resources} 条资源）  示例原始标题：{sample}"


def build_user_message(entries: list[str]) -> str:
    return USER_TEMPLATE.format(entries="\n".join(entries), tool=TOOL_NAME, count=len(entries))
