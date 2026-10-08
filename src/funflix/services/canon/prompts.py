"""归一裁决的 prompt 与工具 schema。

这里有**两套**，各管一种残局，共用 `title_canon` 这张表：

- 归一（`SYSTEM_PROMPT` / `TOOL_SCHEMA`）：回答"这一堆字面相似的脏标题里，
  哪些是同一部作品"。只对 ≥2 个候选项的块有意义。改它要升
  `CANON_PROMPT_VERSION`。
- 分类（`CLASSIFY_SYSTEM_PROMPT` / `CLASSIFY_TOOL_SCHEMA`）：回答"这个孤立的
  键是什么类型"。补的是单候选项块 —— 占全部块的 99.2%，归一那一套按设计
  不碰它们。改它要升 `CANON_CLASSIFY_PROMPT_VERSION`。

两个版本号分开是因为落库的行按 `prompt_version` 留痕：改了分类 prompt 不该
让几千条归一裁决看起来像是旧版本判的，反之亦然。

和 `extract/llm/prompts.py` 是另一回事，不要混：那一套回答"这段文案里有哪些
作品和链接"，跑在数据入库之前；这里两套都跑在入库之后。
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
                            #: `year` **故意**既不在 `required` 里、也不在
                            #: SYSTEM_PROMPT 正文里提 —— 生产库 5889 条裁决里只有
                            #: 16 条带年份，这是设计的结果，不是漏了。
                            #:
                            #: 年份参与 Work 的唯一约束 `(norm_key, media_type,
                            #: year)`，猜错一年就把同一部作品劈成两个 Work，和误并
                            #: 一样难修；而上映年份正是模型最容易记错的字段。规则从
                            #: 标题原文抽的年份（`流浪地球 (2019)`）来源可信，
                            #: `lookup.py` 也是「裁决里是 0 就用规则抽的」这个顺序。
                            #: 所以这一列由规则主导，模型只在确信时顺手补一个。
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


# ---------------------------------------------------------------------------
# 分类裁决（单候选项块）
# ---------------------------------------------------------------------------
#
# 上面那套回答"这一堆键里哪些是同一部作品"，需要块内有候选可比。可生产库
# 185,285 个候选块里 183,814 个（99.2%）只有**一个**候选项 —— `resolver.py`
# 的 `len(entries) < 2: continue` 把它们全跳过了，于是 64,668 个 `media_type`
# 还是 unknown 的键里，64,519 个（99.8%）永远等不到裁决。规则那边也到顶了：
# `guess_media_type` 对 4,000 行抽样 100% 返回 unknown，因为短剧标题
# （`沉默不语的顾小姐`、`飞鸥不下`）压根不带类型信号。
#
# 这一套就是去补那 6.4 万行的，**但刻意不问 `work_title`**：孤立的键本来就
# 没有可并的对象，给模型一个写标题的字段只会凭空制造误并的机会（它可能把
# `大主宰` 和同一批里碰巧出现的 `天命大主宰` 写成同一个名字）。模型在这里
# 没有表达归并的渠道，所以这条路**结构性地**不可能误并 —— 这比在 prompt 里
# 叮嘱"不要并"可靠得多。身份仍由规则定，见 `lookup.py` 的
# `work_title = canon.work_title or title`。

CLASSIFY_TOOL_NAME = "submit_canon_types"

CLASSIFY_SYSTEM_PROMPT = """\
你是影视资源库的作品类型标注器。输入是一批**彼此无关**的标题键，\
每一个都是库里一部独立的作品。你只做一件事：给每一个键标出它的类型。

## 不要做的事

- **不要归并**。这批键之间没有任何关系，字面相似也只是巧合 ——\
`大主宰` 和 `天命大主宰` 是两部不同的作品。你也没有任何字段可以表达归并。
- **不要改写标题**。作品名由规则层决定，不是这一步的职责。
- `key` 必须**原样照抄**，不要清洗、不要补全。

## 类型

`media_type`：movie（电影）、tv（电视剧/短剧/网剧）、anime（动漫/国漫）、\
variety（综艺）、documentary（纪录片）、book（小说/电子书）、comic（漫画）、\
other（课程/软件/其它非影视）、unknown（判断不了）。

**判断不了就填 unknown，不要猜。** 填 unknown 的键会留在原处等以后再判，\
代价只是晚一点；猜错会把一部剧永久标成电影，而那要靠人工才能发现。

这批键大量来自网盘分享频道，其中**中文竖屏短剧占很大比例** ——\
`闪婚后发现老公是首富`、`沉默不语的顾小姐` 这类几十到上百集、每集几分钟的\
作品，类型是 **tv**，不是 movie。片名像一句话、带强情绪钩子的，基本都是短剧。

## 不是作品

`is_junk` 为 true 的情况：网盘按钮文案（`夸克`、`查看资源`）、表格列名、\
纯数字、提取码、分享 ID、整页抓取残渣（`描述大主宰导演马建平编剧…`、\
`post via api service`）、网盘客户端安装包、频道公告与广告、\
**频道的类目或合集标题**（`07月新番`、`最新电影合集`、`电子书打包` ——\
它们不是某一部作品，而是一批作品的入口）。规则层已经拦掉了明显的，\
留给你的是需要读懂语义才看得出来的。`is_junk=true` 时 `media_type` 填 unknown。

## 输出要求

1. **输入的每一个 key 都必须出现在输出里**，一个不能少，一个不能多。
2. `key` 字段原样照抄输入里的 key。
3. `confidence` 填 0-1，表示你对这条标注的确信程度。
"""

CLASSIFY_USER_TEMPLATE = """\
## 待标注的标题键

下面每一行是一个**独立作品**，彼此无关。格式：

    <key>  （出现 N 行 / M 条资源）  示例原始标题：…

{entries}

请调用 {tool} 提交标注，输入的 {count} 个 key 每一个都要有一条对应的结果。\
"""

#: 和 `TOOL_SCHEMA` 的差别就是这一步的全部安全性所在：**没有 `work_title`、
#: 没有 `season`**。
#:
#: 少 `work_title` 是为了堵死误并（见上面的说明）。少 `season` 是因为孤立的键
#: 没有"整部作品的通名覆盖了好几季"这个判断依据 —— 季号由 `lookup.py` 的
#: `extract_season(title)` 逐条判定更准，模型在这里填任何数字都是把一条分享的
#: 季号钉到整个键上。
CLASSIFY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": CLASSIFY_TOOL_NAME,
        "description": "提交每个标题键的作品类型标注",
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
                            "media_type": {"type": "string", "enum": _MEDIA_TYPES},
                            #: 和 `TOOL_SCHEMA` 里的 `year` 同一个取舍：不进
                            #: `required`、正文也不提，模型确信时顺手补一个。
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
                        "required": ["key", "media_type", "is_junk", "confidence"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["decisions"],
            "additionalProperties": False,
        },
    },
}


def build_classify_message(entries: list[str]) -> str:
    return CLASSIFY_USER_TEMPLATE.format(
        entries="\n".join(entries), tool=CLASSIFY_TOOL_NAME, count=len(entries)
    )
