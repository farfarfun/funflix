"""知识沉淀：把 LLM 已经买到的作品身份，免费复用给字面相同的未裁决键。

LLM 读完一整块候选项之后判出「这些键都属于 `大主宰`」。那个结论里顺带确立了
一件事：**`大主宰` 本身就是一部作品的规范名**。所以后面再冒出一个
`series_norm_key` 恰好就等于 `大主宰` 的未裁决键时，它的归属已经不需要再问
模型了 —— 答案就在库里。

这一步因此是纯 SQL 的，不发任何调用，每轮 `canon resolve` 开头跑一遍。

## 为什么只认**字面完全相等**

这一步的全部安全性都压在「相等」上。放宽一点点就会误并，而误并不可逆：

- `天命大主宰` 不是 `大主宰`（前缀不同就是另一部）
- `大主宰动态漫` 不是 `大主宰`（同名不同媒介，资源不该混在一起）
- `从大主宰开始打卡` 是小说

这几个的区分只有读懂语义才做得到，正是花钱请模型的理由（见
`canon/resolver.py` 的模块说明）。所以这里不做前缀、不做包含、不做编辑距离 ——
那等于用一条正则去冒充模型的判断。季号后缀不必特殊处理：
`series_norm_key` 已经把 `大主宰 第3季` / `大主宰第二季` / `大主宰年番` 都收敛
成 `大主宰` 了，走到这一步的键本身就已经是剥过季的系列身份。

## 继承哪些字段

- `work_title` / `work_norm_key`：继承。这是这一步的全部目的。
- `media_type`：继承。类型是**作品级**属性，第 1 季是动漫第 3 季不会变成电影。
  而且它有实际后果 —— `services/search.VIDEO_MEDIA_TYPES` 之外的类型默认不进
  搜索结果，让这些键停在 `unknown` 等于把判断结果扔了。
- `season`：写 `None`，**不继承**。理由同 `canon/lookup.py::pending_row`：
  这一列的语义是「这个键锁定了哪一季」，而光杆作品名没有锁定任何一季。
  继承某个兄弟行的季号，就等于把整部剧钉死在那一季上。
- `year`：**不继承**，留 `UNKNOWN_YEAR`。年份是**季级**属性（`大主宰` 第 1 季
  2020、第 3 季 2023），兄弟行的年份对光杆键来说是错的。留空反而正确 ——
  `lookup.resolve_target` 会用这一行自己判出来的年份补上。
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import MediaType
from funflix.models.canon import CANON_PROMPT_VERSION, CanonState, TitleCanon
from funflix.models.media import UNKNOWN_YEAR
from funflix.services.text.normalize import looks_like_junk_title

#: 写进 `title_canon.model` 的来源标记。不留空也不冒充模型名 —— 这一行是
#: 规则按已有裁决推出来的，审计时必须能和真的 LLM 裁决区分开（判错了要回溯
#: 到底是模型判错还是沉淀规则传错）。
SEDIMENT_MODEL = "sediment:known-work"


@dataclass(slots=True)
class SedimentReport:
    #: 已裁决、非垃圾、有作品键的行数（沉淀的知识来源）
    sources: int = 0
    #: 去重后的已知作品键数
    known_works: int = 0
    #: 字面命中已知作品键的未裁决键数
    matched: int = 0
    #: 真正写进库的条数（`apply=False` 时为 0）
    settled: int = 0
    applied: bool = False
    samples: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _KnownWork:
    """一个已知作品键沉淀下来的可继承属性。"""

    work_title: str
    media_type: MediaType


async def _known_works(session: AsyncSession, report: SedimentReport) -> dict[str, _KnownWork]:
    """按 `work_norm_key` 聚合已裁决行，得出每个已知作品的规范名与类型。"""
    rows = list(
        await session.scalars(
            select(TitleCanon).where(
                TitleCanon.status == CanonState.DECIDED,
                TitleCanon.is_junk.is_(False),
                TitleCanon.work_norm_key.isnot(None),
                TitleCanon.work_norm_key != "",
                TitleCanon.work_title.isnot(None),
            )
        )
    )
    report.sources = len(rows)

    titles: dict[str, Counter[str]] = {}
    types: dict[str, Counter[MediaType]] = {}
    for row in rows:
        key = row.work_norm_key or ""
        titles.setdefault(key, Counter())[row.work_title or ""] += 1
        if row.media_type is not MediaType.UNKNOWN:
            types.setdefault(key, Counter())[row.media_type] += 1

    known: dict[str, _KnownWork] = {}
    for key, title_counter in titles.items():
        # 同一个作品键底下可能挂着几种写法（不同字面洗出同一个键）。取出现
        # 次数最多的，同票按字面排序 —— 必须是确定性的，否则同一批数据重跑
        # 会写出不同的 `work_title`，搜索结果跟着抖。
        title = min(title_counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if not title:
            continue
        type_counter = types.get(key)
        media_type = MediaType.UNKNOWN
        if type_counter:
            media_type = min(type_counter.items(), key=lambda kv: (-kv[1], kv[0].value))[0]
        known[key] = _KnownWork(work_title=title, media_type=media_type)

    report.known_works = len(known)
    return known


async def settle_known_works(
    session: AsyncSession, *, apply: bool = False
) -> tuple[SedimentReport, set[str]]:
    """把未裁决键中字面等于已知作品键的那些，就地判成 `decided`。

    Args:
        session: 数据库会话。`apply=True` 时本函数自己 commit。
        apply: 为假时只统计命中数、一个字段都不写（`canon resolve --dry-run`
            用的就是这条路）。

    Returns:
        `(报告, 这一轮沉淀的键集合)`。键集合给 `resolve_canon` 并进 `done`，
        这样本轮就不会再把它们送去烧钱 —— `apply=False` 时也要并，否则
        dry-run 报出来的待送块数比实际偏多。
    """
    report = SedimentReport(applied=apply)
    known = await _known_works(session, report)
    if not known:
        return report, set()

    rows = list(
        await session.scalars(
            select(TitleCanon).where(
                TitleCanon.status != CanonState.DECIDED,
                TitleCanon.norm_key.in_(list(known)),
            )
        )
    )
    # 兜底：模型给出的规范名万一洗完是垃圾，别让它变成一个吸收脏数据的黑洞
    # （同 `lookup.resolve_target` 对坏裁决行的处理）。
    rows = [row for row in rows if not looks_like_junk_title(known[row.norm_key].work_title)]
    report.matched = len(rows)
    report.samples = [f"{row.norm_key} → {known[row.norm_key].work_title}" for row in rows[:20]]
    if not apply or not rows:
        return report, {row.norm_key for row in rows}

    now = dt.datetime.now(dt.UTC)
    for row in rows:
        work = known[row.norm_key]
        row.work_norm_key = row.norm_key
        row.work_title = work.work_title[:500]
        # 见模块说明：季和年都不继承。
        row.season = None
        row.year = UNKNOWN_YEAR
        row.media_type = work.media_type
        row.is_junk = False
        row.status = CanonState.DECIDED
        # 不是模型给的置信度，留空而不是编一个 1.0
        row.confidence = None
        row.model = SEDIMENT_MODEL
        row.prompt_version = CANON_PROMPT_VERSION
        row.decided_at = now

    await session.commit()
    report.settled = len(rows)
    return report, {row.norm_key for row in rows}
