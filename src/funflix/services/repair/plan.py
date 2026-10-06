"""判定「这一行 media 按现在的规则该是什么样」—— 纯函数，不碰数据库。

和 `canon/lookup.py` 同一个定位：真正的 IO（分页扫 media、批量查 `title_canon`、
查目标身份有没有被占）留在 `scan.py`，这里只做判定，于是能脱开数据库单测。
判定错一次就是一次误删或误并，这类代码必须能被廉价地测穷。

## 目标值从哪来

**复用 `canon/lookup.py::resolve_target`**，不另写一套规则。这是本模块最重要的
一条约束：修复的终点必须和 **parse 现在会产出的结果**一字不差，否则两条路
会互相拆台 —— repair 把一行改成 A，下一条分享进来 parse 又把它改回 B，
然后下一轮 scan 再检出一个任务，无限循环。

## 为什么只能从已清洗的标题重算

`media.original_title` 全库是空的（本次实查），原始文本只在
`raw_document.content` 和 `resource.title_raw` 上，而一行 media 挂着几十条
resource，哪一条的原始标题算数没有定论。所以这里的入参是**旧规则的清洗产出**
`media.title`，再洗一遍。

这是可行的，因为 `clean_title` 的每一步都是**减法**：对已经干净的标题是空操作，
而新补进词表的噪声词照样能剥掉。代价是旧规则**洗坏**的行恢复不了 —— 那类
只能走深层重解析（`requeue.py`）。和 `canon/purge.py::is_junk_media_title`
是同一个取舍，口径必须一致。

## 类型和年份只补不改

标题的信息量比正文少：一行的 `media_type` 很可能是抽取器从正文
`类型:电影` 判出来的，而只看标题会得到 `UNKNOWN`。拿标题的判定去覆盖，
等于用更少的信息推翻更多的信息 —— 一轮 scan 就能把全库的类型刷成未知。

所以是单向的，见 `_retype` / `_reyear`。唯一的例外是书刊：`_BOOK_SIGNAL_RE`
命中 `作者:` 是决定性信号，这正是整套机制的第一个用例（4,399 行小说分享
现在挂在影视类型上污染搜索）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from funflix.base.enums import MediaType
from funflix.models.canon import TitleCanon
from funflix.models.media import UNKNOWN_YEAR
from funflix.models.repair import RepairKind, RepairSymptom
from funflix.services.canon.lookup import resolve_target
from funflix.services.text.normalize import (
    clean_title,
    extract_year,
    guess_media_type,
    looks_like_junk_title,
    series_norm_key,
)


@dataclass(frozen=True, slots=True)
class MediaFacts:
    """扫描一行 media 需要的全部输入。

    刻意不收 `Media` 实例：`scan.py` 走的是 `select(列...)` 分页而不是加载
    ORM 对象（89 万行全部实例化会把内存吃光），而纯函数层也不该依赖
    会话状态。
    """

    title: str
    media_type: MediaType
    year: int
    season: int
    resource_count: int
    #: 所属 Work 的 `norm_key`。`None` = 这一行还没归属（迁移 B 之前的遗留）。
    work_norm_key: str | None


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """一行 media 的修复方案。`kind` 决定用哪条执行路径。"""

    kind: str
    symptom: str
    #: 和 `RepairTask.payload` 同一个类型 —— 它原样进 JSON 列，再被
    #: `apply.py` 原样读回来，中间不该有一次类型转换。
    payload: dict[str, Any] = field(default_factory=dict)


def _retype(current: MediaType, detected: MediaType) -> MediaType:
    """类型只补不改，唯一例外是书刊。理由见模块说明。"""
    if detected is MediaType.BOOK or detected is MediaType.COMIC:
        # 书刊信号是决定性的：`作者:` / `epub` 不会出现在影视分享里
        # （`原著作者:` 已被 `_BOOK_SIGNAL_RE` 的负向回顾排掉）。
        return detected
    if detected is MediaType.UNKNOWN:
        return current
    if current is MediaType.UNKNOWN:
        return detected
    # 两边都是具体的影视类型 —— 标题的信息量不足以推翻正文的判定。
    return current


def _reyear(current: int, detected: int | None) -> int:
    """年份同理：只在当前未知时补。"""
    if current != UNKNOWN_YEAR or detected is None:
        return current
    return detected


def plan_repair(facts: MediaFacts, canon: TitleCanon | None) -> RepairPlan | None:
    """算出这一行该怎么修，不需要修就返回 `None`。

    Args:
        facts: 这一行的当前值。
        canon: 按 `series_norm_key(clean_title(facts.title))` 查到的裁决行。
            键空间必须是 `series_norm_key`，理由见 `canon/lookup.py`。

    判定顺序是**先删后改**，命中即止：垃圾行和空壳先摘出去，它们不该再参与
    后面的重挂和多合一（否则会把垃圾并进正常作品）。

    返回 `None` 是**常态**：规则没变时每一行都该落到这里，于是 scan 一个任务
    都不建、一个字都不写。这是整套机制能挂在流水线上每轮跑的前提。
    """
    new_title = clean_title(facts.title)

    if looks_like_junk_title(new_title):
        return RepairPlan(kind=RepairKind.DELETE, symptom=RepairSymptom.JUNK)

    if facts.resource_count == 0:
        # 一条资源都没有的行对用户是死链，留着只会让搜索结果变脏。
        # resource 侧的关联已经没了，所以删它不会丢任何链接。
        return RepairPlan(kind=RepairKind.DELETE, symptom=RepairSymptom.EMPTY_SHELL)

    # 类型和年份在 `facts.title` 上判，**不是** `new_title` —— 两者差一次清洗，
    # 而清洗正好会把判据剥掉：
    #   `全民攻防:我有签到系统 作者:奏光 txt` → 洗完是 `全民攻防:我有签到系统`，
    #   `作者:` 这个书刊信号没了，判出来是 unknown 而不是 book。
    #   `流浪地球 (2019)` → 洗完是 `流浪地球`，年份也没了。
    # 库里存着的那个标题是我们手上最接近原文的东西（`original_title` 全库为空，
    # 见模块说明），判据只能从它身上取。
    new_type = _retype(facts.media_type, guess_media_type(facts.title, facts.title))
    new_year = _reyear(facts.year, extract_year(facts.title))

    target = resolve_target(
        title=new_title,
        media_type=new_type,
        year=None if new_year == UNKNOWN_YEAR else new_year,
        canon=canon,
    )
    if target.is_junk:
        # LLM 裁决说这个键不是作品。和规则判出的 junk 走同一条删除路径。
        return RepairPlan(kind=RepairKind.DELETE, symptom=RepairSymptom.JUNK)

    # `resolve_target` 对 decided 的裁决会用裁决里的类型/年份覆盖规则的判定，
    # 所以最终值要从 target 上取，不能用上面的 new_type / new_year。
    fields: dict[str, Any] = {
        "title": new_title,
        "media_type": target.media_type.value,
        "year": target.year,
    }
    title_drift = (
        new_title != facts.title
        or target.media_type is not facts.media_type
        or target.year != facts.year
    )

    key_drift = target.work_norm_key != facts.work_norm_key or target.season != facts.season
    if key_drift and target.work_norm_key:
        # 标题的修改**一并**交给 rehome：`assign_identities` 的 `extra_titles`
        # 支持在搬迁的同一步刷标题，所以同一行不需要拆成两个任务，也不会
        # 出现「改完标题下一轮才搬家」的两轮收敛。
        return RepairPlan(
            kind=RepairKind.REHOME,
            symptom=RepairSymptom.KEY_DRIFT,
            payload={
                **fields,
                "work_norm_key": target.work_norm_key,
                "work_title": target.work_title[:500],
                "season": target.season,
            },
        )

    if title_drift:
        return RepairPlan(
            kind=RepairKind.RETITLE, symptom=RepairSymptom.TITLE_DRIFT, payload=fields
        )

    return None


def plan_key(title: str) -> str:
    """这一行该用哪个 `title_canon.norm_key` 去查裁决。

    单独暴露出来有两个用处：`scan.py` 批量预查裁决（一页 500 行攒成一次 IN
    查询，不能每行发一次），以及 `scan` / `apply` 的 `--key` 过滤。键必须和
    `plan_repair` 里 `resolve_target` 看到的一致 —— 三处用同一个函数，
    就不会出现「scan 建的任务 apply 筛不到」。

    入参是 `media.title` 而不是 `MediaFacts`：`--key` 过滤只拿到标题一列，
    为它凑一个完整的 facts 没有意义。
    """
    return series_norm_key(clean_title(title))
