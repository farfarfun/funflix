"""阶段 3：把规则搞不定的残局交给 LLM 裁决，结果落 `title_canon`。

规则（阶段 2）已经把 92.9 万行并成 44 万个 `series_norm_key`。剩下的问题
正则**原理上**解决不了，必须读懂语义：

- `大主宰2` 和 `大主宰` 是同一部的两季，还是两部独立作品？（是前者）
- `天命大主宰` 和 `大主宰` 呢？（是后者，必须分开）
- `大主宰:我荒古圣体,当为天帝! 作者:墨之所想` 是那部动漫吗？（不是，是小说）

## 分块：`block_key` 粗分，`series_norm_key` 当候选项

一个候选块 = 一个 `block_key`，块里的候选项是落在它底下的各个
`series_norm_key`。`block_key` 比 `series_norm_key` 激进（连末尾光杆数字和
罗马数字一起摘），所以 `大主宰` / `大主宰2` / `大主宰ii` 会落进同一块 ——
这正是想要的：**分块宁宽不紧**。该在一块的没在一块，模型根本没机会纠正；
分宽了只是多给几个候选，代价仅仅是 token。

**归一只送 ≥2 个候选项的块**（实测 1,471 个）。单候选项的块没有可并的对象，
拿去问"哪些是同一部"纯烧钱。

## 阶段 2：单候选项块只标类型

上面那句话有个代价，拖到现在才付：生产库 185,285 个块里 **183,814 个
（99.2%）只有一个候选项**，归一那一路一个都不碰，于是 64,668 个
`media_type` 还是 unknown 的键里，**64,519 个（99.8%）永远等不到裁决**。
规则那边也到顶了 —— `guess_media_type` 对 4,000 行抽样 100% 返回 unknown，
因为短剧标题（`沉默不语的顾小姐`、`飞鸥不下`）压根不带类型信号。

所以加了第二个阶段，**只问类型、不问作品名**：孤立的键本来就没有可并的对象，
给模型一个写标题的字段只会凭空制造误并的机会。它在 `CLASSIFY_TOOL_SCHEMA`
里没有表达归并的字段，所以这条路**结构性地**不可能误并 —— 比在 prompt 里
叮嘱"不要并"可靠。身份仍由规则给（`lookup.py` 的
`canon.work_title or title`），落下来的行 `work_norm_key` 是 NULL。

两个阶段串行、共用一趟全表扫描和一个限速器，额度各自独立
（`limit` / `classify_limit`）。串行是必须的，理由见 `_singles_pending`。

## 一次调用一个块

`CANON_PROMPT_VERSION` 那边的计划里写过"小块打包进同一次调用"省 token，
这里**没这么做**：把互不相关的块塞进一次调用，等于主动制造跨块误并的机会，
而误并不可逆。4,899 次调用的钱比修一次误并便宜得多。

超大块按 `MAX_ENTRIES_PER_CALL` 拆页，但**顺序跑**并把前几页已经定下来的
作品名回传给模型（见 `_decided_context`）—— 不然第 2 页可能给出
`大主宰 年番` 而第 1 页给的是 `大主宰`，字面不同就并不到一起去。

## 先沉淀，再花钱

每轮开头先跑 `canon/sediment.py`：上几轮买到的作品身份，能免费判掉那些
**字面恰好等于某个已知作品键**的未裁决键（模型判出「这些键都属于 `大主宰`」
的同时就已经确立了 `大主宰` 是个规范作品名）。沉淀掉的键并进 `done`，
本轮不再送进调用。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from farlog import getLogger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import MediaType
from funflix.models import Media
from funflix.models.canon import (
    CANON_CLASSIFY_PROMPT_VERSION,
    CANON_PROMPT_VERSION,
    CanonState,
    TitleCanon,
)
from funflix.models.media import UNKNOWN_YEAR
from funflix.services.canon import prompts
from funflix.services.canon import sediment as sediment_mod
from funflix.services.extract.llm.client import (
    LLMCallError,
    LLMClient,
    OpenAICompatClient,
)
from funflix.services.text.normalize import (
    block_key,
    clean_title,
    guess_media_type,
    looks_like_junk_title,
    series_norm_key,
)

logger = getLogger("funflix")

#: 一次调用最多塞多少个候选项。再多模型开始漏项、`key` 照抄也开始出错。
MAX_ENTRIES_PER_CALL = 50

#: 并发调用数。受网关限流约束，不是 CPU 约束。
DEFAULT_CONCURRENCY = 8

#: 两次调用的**起跑时刻**至少隔这么多秒。
#:
#: 网关限的是速率，不是并发数 —— 并发压到 4 仍然有四分之一的调用吃到
#: `429 Request Rate Reaches Maximum Limit`（实测 run 37693106252：200 次调用
#: 10 分钟跑完 ≈ 20 次/分，其中 50 次失败）。SDK 自带的 5 次指数退避管不了
#: 这个：几个协程各自退避、退完又一起撞回来，把速率重新顶上去。
#:
#: 429 是白走的进度：请求没被处理，这一页的候选项整页丢掉、留到下一轮重来。
#: 4 秒 ≈ 15 次/分，正好是那一轮**实际跑通**的速率。
MIN_CALL_INTERVAL = 4.0

#: 分类标注里 `is_junk=true` 的采纳门槛。
#:
#: 高得不像个阈值是故意的：`is_junk` 到了 `canon/apply.py` 是**真删 media 行**，
#: 而分类这一路一轮要送上万个孤立键 —— 模型偏激进一点，删除量就是四位数且
#: 不可逆。归一那一路不设这个门槛，因为它一次只送一个块、几十个键，量级差三个
#: 数量级。不够格的 junk 就当这条没信息，留在 `pending` 下轮再问。
CLASSIFY_JUNK_MIN_CONFIDENCE = 0.9

#: 落库的分段大小，一段一个事务。见 `_persist` 为什么必须分段。
PERSIST_CHUNK_SIZE = 500

#: 一段撞上并发插入后最多重试几次。重试几乎总是一次就过（重查就看见那行了），
#: 给 3 次是为了容下"连着被插了两个不同的键"这种小概率叠加。
PERSIST_CONFLICT_ATTEMPTS = 3

#: 年份的合理区间，超出即视为模型瞎填。与 `llm/extractor` 同一个口径。
_YEAR_MIN, _YEAR_MAX = 1900, 2100

SAMPLE_LIMIT = 20


@dataclass(slots=True)
class CanonEntry:
    """候选块里的一个候选项 —— 一个 `series_norm_key` 及其规模。

    `rows` / `resources` 是给模型的**权重信号**：360 行 / 1,882 条资源的
    `大主宰` 显然是主干，1 行 0 资源的 `大主宰 edr` 显然是它的噪声变体。
    不给规模的话模型容易把两者当成平等的两部作品。
    """

    key: str
    rows: int = 0
    resources: int = 0
    sample: str = ""
    #: 这个键底下**已经有行带着类型**了 —— 抽取那一步从整篇文案判出来的
    #: （`extract/rule.py` 给 `guess_media_type` 的是 `segment.text`，比标题
    #: 信息多得多）。分类阶段靠它避开已有答案的键，见 `_singles_pending`。
    typed: bool = False


@dataclass(slots=True)
class CanonDecision:
    key: str
    work_title: str | None
    work_norm_key: str | None
    season: int | None
    media_type: MediaType
    year: int
    is_junk: bool
    confidence: float | None


@dataclass(slots=True)
class ResolveReport:
    scanned: int = 0
    #: 开跑前靠「字面等于已知作品键」免费判掉的键数（见 `canon/sediment.py`）
    settled: int = 0
    #: 全部候选块数（含单候选项的）
    blocks: int = 0
    #: 够格送 LLM 的块数（≥2 个候选项且还有未裁决的 key）
    blocks_eligible: int = 0
    blocks_sent: int = 0
    calls: int = 0
    calls_failed: int = 0
    decided: int = 0
    junk: int = 0
    #: 模型返回但没通过校验、因此没进库的条数
    rejected: int = 0
    #: 送进去却没在返回里出现的 key
    missing: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    dry_run: bool = True
    samples: list[str] = field(default_factory=list)

    # --- 阶段 2：单候选项块的类型标注（见 `_singles_pending`）-----------------
    #: 只有一个候选项、因此归一那一路按设计不碰的块数
    singles: int = 0
    #: 其中还缺类型、够格送标注的 key 数（规则判得出类型的不送）
    singles_eligible: int = 0
    #: 本轮实际送出的 key 数
    singles_sent: int = 0
    classify_calls: int = 0
    classify_calls_failed: int = 0
    #: 标注出类型、已落库的条数
    classified: int = 0
    #: 标注成 junk 的条数
    classify_junk: int = 0
    #: 模型自己也判不出（返回 unknown）因此**故意不落库**的条数，见
    #: `validate_classifications`
    classify_unknown: int = 0
    classify_rejected: int = 0
    classify_missing: int = 0
    classify_samples: list[str] = field(default_factory=list)


async def _scan_blocks(session: AsyncSession, report: ResolveReport) -> dict[str, list[CanonEntry]]:
    """扫全表，按 `block_key` → `series_norm_key` 聚合。

    只读，一趟流式扫完。跳过规则已经判成垃圾的行 —— 它们在阶段 1 就该被删掉，
    这里再拦一道是为了让 resolve 在没跑过 purge 的库上也不会把垃圾送去烧钱。
    """
    blocks: dict[str, dict[str, CanonEntry]] = {}
    rows = await session.stream(select(Media.title, Media.resource_count, Media.media_type))
    async for title, resource_count, media_type in rows:
        report.scanned += 1
        cleaned = clean_title(title or "")
        if looks_like_junk_title(cleaned):
            continue

        key = series_norm_key(title or "")
        if not key:
            continue

        entries = blocks.setdefault(block_key(title or ""), {})
        entry = entries.get(key)
        if entry is None:
            entry = CanonEntry(key=key, sample=(title or "")[:120])
            entries[key] = entry
        entry.rows += 1
        entry.resources += resource_count or 0
        if media_type is not MediaType.UNKNOWN:
            entry.typed = True

    report.blocks = len(blocks)
    # 候选项按规模倒序：拆页时主干必须落在第 1 页，后面的页才有锚可以对。
    return {
        block: sorted(entries.values(), key=lambda e: (-e.resources, -e.rows, e.key))
        for block, entries in blocks.items()
    }


def _coerce_year(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return UNKNOWN_YEAR
    return value if _YEAR_MIN <= value <= _YEAR_MAX else UNKNOWN_YEAR


def _coerce_season(value: Any) -> int | None:
    """季号。`None` 是有意义的取值 —— 见模块与 `merge` 的说明。

    `None` 表示「这个 key 没锁定某一季」，merge 时**不覆盖**规则逐行判出的
    季号。所以不能把越界值悄悄折成 0（那是"确定无季"），只能折成 None。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 0 <= value <= 99 else None


def validate_decisions(
    payload: dict[str, Any], entries: list[CanonEntry]
) -> tuple[list[CanonDecision], dict[str, int]]:
    """把模型返回校验成裁决列表。校验而非信任。

    拦四类问题，每一类都计数而不是静默吞掉：

    - `key` 不在送进去的集合里 —— 模型编了个候选项，或者把 key 洗过一遍
    - 同一个 `key` 返回多条 —— 只认第一条
    - 非 junk 却没给 `work_title` —— 落库会得到一个没有归属的裁决
    - 送进去却没返回 —— 留在 `pending`，下次重跑

    `work_norm_key` 在这里**本地算**，不问模型：两个 key 只要被判成字面相同的
    `work_title`，归一键就必然相同。让模型自己编键会出现"标题一样、键不一样"
    的自相矛盾结果，那种数据进了库就是两个 Work。
    """
    expected = {e.key for e in entries}
    stats = {"unknown_key": 0, "duplicate_key": 0, "empty_work_title": 0, "missing": 0}

    raw = payload.get("decisions")
    if not isinstance(raw, list):
        raw = []

    decisions: list[CanonDecision] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            stats["unknown_key"] += 1
            continue

        key = str(item.get("key") or "").strip()
        if key not in expected:
            stats["unknown_key"] += 1
            continue
        if key in seen:
            stats["duplicate_key"] += 1
            continue
        seen.add(key)

        is_junk = bool(item.get("is_junk"))
        title = str(item.get("work_title") or "").strip()
        if not is_junk and not title:
            stats["empty_work_title"] += 1
            continue

        work_key = series_norm_key(title) if title else None
        if not is_junk and not work_key:
            # 模型给的标题清洗完是空的（整条都是噪声词）—— 没法当作品身份
            stats["empty_work_title"] += 1
            continue

        media_type = MediaType.UNKNOWN
        raw_type = item.get("media_type")
        if isinstance(raw_type, str):
            try:
                media_type = MediaType(raw_type.strip().lower())
            except ValueError:
                media_type = MediaType.UNKNOWN

        confidence = item.get("confidence")
        decisions.append(
            CanonDecision(
                key=key,
                work_title=title or None,
                work_norm_key=None if is_junk else work_key,
                season=_coerce_season(item.get("season")),
                media_type=media_type,
                year=_coerce_year(item.get("year")),
                is_junk=is_junk,
                confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            )
        )

    stats["missing"] = len(expected - seen)
    return decisions, stats


def validate_classifications(
    payload: dict[str, Any], entries: list[CanonEntry]
) -> tuple[list[CanonDecision], dict[str, int]]:
    """把分类标注校验成裁决列表。

    和 `validate_decisions` 的三个关键差别：

    1. **`work_title` / `work_norm_key` 一律留 `None`**，`season` 也是。schema
       里根本没有这些字段（见 `prompts.CLASSIFY_TOOL_SCHEMA`），这一路不表达
       作品身份，只补类型。身份由 `lookup.py` 的
       `work_title = canon.work_title or title` 从规则那边拿。
    2. **模型返回 unknown 的直接丢掉，不落库。** 落了反而有害：`_persist` 会把
       这一行从 `pending` 翻成 `decided`，而 `resolve_canon` 和 `sediment` 都按
       `status` 判断"还要不要处理这个键" —— 一个 unknown 的 decided 行等于把
       这个键永久钉死在没有类型的状态上，以后换了更强的模型也捞不回来。
       丢掉的话它还是 `pending`，下一轮还能再问。
    3. **junk 要 `confidence >= CLASSIFY_JUNK_MIN_CONFIDENCE` 才采纳。** 归一
       那一路一次只送一个块、几十个键，这一路一轮要送上万个孤立键，而
       `is_junk` 在 `canon/apply.py` 那边是**真删 media 行**。模型稍微偏激进，
       删除量就是四位数且不可逆。不够格的 junk 退化成"这条没信息"，跟 unknown
       一样丢掉。
    """
    expected = {e.key for e in entries}
    stats = {
        "unknown_key": 0,
        "duplicate_key": 0,
        "no_signal": 0,
        "junk_low_confidence": 0,
        "missing": 0,
    }

    raw = payload.get("decisions")
    if not isinstance(raw, list):
        raw = []

    decisions: list[CanonDecision] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            stats["unknown_key"] += 1
            continue

        key = str(item.get("key") or "").strip()
        if key not in expected:
            stats["unknown_key"] += 1
            continue
        if key in seen:
            stats["duplicate_key"] += 1
            continue
        seen.add(key)

        raw_confidence = item.get("confidence")
        confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else None

        is_junk = bool(item.get("is_junk"))
        if is_junk and (confidence is None or confidence < CLASSIFY_JUNK_MIN_CONFIDENCE):
            stats["junk_low_confidence"] += 1
            continue

        media_type = MediaType.UNKNOWN
        raw_type = item.get("media_type")
        if isinstance(raw_type, str):
            try:
                media_type = MediaType(raw_type.strip().lower())
            except ValueError:
                media_type = MediaType.UNKNOWN

        if not is_junk and media_type is MediaType.UNKNOWN:
            # 模型也判不出来 —— 留在 pending，见 docstring 第 2 条。
            stats["no_signal"] += 1
            continue

        decisions.append(
            CanonDecision(
                key=key,
                work_title=None,
                work_norm_key=None,
                season=None,
                media_type=media_type,
                year=_coerce_year(item.get("year")),
                is_junk=is_junk,
                confidence=confidence,
            )
        )

    stats["missing"] = len(expected - seen)
    return decisions, stats


def _decided_context(decided: dict[str, CanonDecision]) -> str:
    """把前几页定下来的作品名回传给模型，让它照抄而不是另起一个写法。"""
    titles = sorted({d.work_title for d in decided.values() if d.work_title})
    if not titles:
        return ""
    listed = "\n".join(f"    {t}" for t in titles)
    return (
        "\n## 这个候选块里已经定下来的作品名\n\n"
        "同一部作品请**原样照抄**下面的写法，不要另起一个：\n\n"
        f"{listed}\n"
    )


class _Pacer:
    """把所有模型调用的起跑时刻排开，两次之间至少隔 `interval` 秒。

    限的是**速率**，跟 `asyncio.Semaphore` 限并发是两件事，两样都要：并发
    决定同时有几个请求在飞（用来盖住单次调用十几秒的延迟），速率决定每分钟
    往网关推几个。只限并发的后果见 `MIN_CALL_INTERVAL`。

    `interval <= 0` 时完全不节流，测试和单组演练用。
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = asyncio.Lock()
        self._next_at: float | None = None

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        # 整段都握着锁：后一个等待者要等前一个睡完才开始算自己的时刻，
        # 这样 N 个并发调用的起跑时刻就是严格等间隔的。
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self._next_at is not None and self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = self._next_at
            self._next_at = now + self._interval


async def _resolve_block(
    client: LLMClient,
    entries: list[CanonEntry],
    report: ResolveReport,
    pacer: _Pacer,
) -> dict[str, CanonDecision]:
    """裁决一个候选块，按需拆页。页之间顺序跑，后面的页能看到前面的结论。"""
    decided: dict[str, CanonDecision] = {}
    for start in range(0, len(entries), MAX_ENTRIES_PER_CALL):
        page = entries[start : start + MAX_ENTRIES_PER_CALL]
        lines = [prompts.format_entry(e.key, e.rows, e.resources, e.sample) for e in page]
        user = prompts.build_user_message(lines) + _decided_context(decided)

        await pacer.wait()
        report.calls += 1
        try:
            result = await client.extract(prompts.SYSTEM_PROMPT, user)
        except LLMCallError as exc:
            # 这一页留在 pending，下次重跑。不让一页失败拖垮整个任务。
            report.calls_failed += 1
            logger.warning(f"归一裁决调用失败，该页留待重跑：{exc}")
            continue

        report.input_tokens += result.input_tokens or 0
        report.output_tokens += result.output_tokens or 0

        page_decisions, stats = validate_decisions(result.payload, page)
        report.rejected += stats["unknown_key"] + stats["duplicate_key"] + stats["empty_work_title"]
        report.missing += stats["missing"]
        for decision in page_decisions:
            decided[decision.key] = decision

    return decided


def _singles_pending(
    blocks: dict[str, list[CanonEntry]], done: set[str], report: ResolveReport
) -> list[CanonEntry]:
    """挑出够格送类型标注的孤立键。

    三道筛子，每一道都是在省钱：

    - **只要单候选项的块**。多候选项的块归一那一路会处理，而它给的裁决信息更全
      （身份 + 季 + 类型），不该在这里先用一个只有类型的裁决把它占掉。
    - **跳过已裁决的键**。一个 `series_norm_key` 可能同时落在一个多候选项块和
      一个单候选项块里（它丢掉尾部的拉丁别名、`block_key` 留着，生产库
      104,527 个键里有 621 个这样），所以这里必须查 `done`，不能假定两路的键
      不相交。
    - **跳过已经有类型的键**（`entry.typed`）。抽取那一步是拿**整篇文案**喂
      `guess_media_type` 的，比这里只有一个标题强得多，它判出来的类型就是答案
      了。这一道筛掉的量最大：不看它的话够格数是 164,431，看了之后
      约 6.4 万 —— 六成的调用本来是白花的。
    - **跳过标题本身就能判出类型的键**。`guess_media_type` 对**标题**也能判出
      来的同样不花钱。实测它对生产库抽样 100% 返回 unknown（短剧标题不带类型
      信号），所以这一道几乎筛不掉东西 —— 留着是为了"规则能干的不交给模型"
      这个顺序不会因为以后规则变强而失效。

    按资源数倒序：额度小的时候先修前台最显眼的那些行。
    """
    out: list[CanonEntry] = []
    for entries in blocks.values():
        if len(entries) != 1:
            continue
        report.singles += 1
        entry = entries[0]
        if entry.key in done or entry.typed:
            continue
        if guess_media_type(entry.sample, entry.sample) is not MediaType.UNKNOWN:
            continue
        out.append(entry)

    out.sort(key=lambda e: (-e.resources, -e.rows, e.key))
    report.singles_eligible = len(out)
    return out


async def _classify_pack(
    client: LLMClient,
    entries: list[CanonEntry],
    report: ResolveReport,
    pacer: _Pacer,
) -> list[CanonDecision]:
    """标注一包孤立的键。一包一次调用，没有拆页也没有跨页上下文。

    `_resolve_block` 那边要拆页、要把前几页定下来的作品名回传，是因为同一个块
    拆开之后两页必须给出**字面相同**的 `work_title` 才能并到一起。这里每个键
    各自独立、又不产出标题，页与页之间没有任何需要对齐的东西 —— 所以包怎么切
    都不影响结果，一包就是一次调用。
    """
    lines = [prompts.format_entry(e.key, e.rows, e.resources, e.sample) for e in entries]
    user = prompts.build_classify_message(lines)

    await pacer.wait()
    report.classify_calls += 1
    try:
        result = await client.extract(prompts.CLASSIFY_SYSTEM_PROMPT, user)
    except LLMCallError as exc:
        report.classify_calls_failed += 1
        logger.warning(f"类型标注调用失败，该包留待重跑：{exc}")
        return []

    report.input_tokens += result.input_tokens or 0
    report.output_tokens += result.output_tokens or 0

    decisions, stats = validate_classifications(result.payload, entries)
    report.classify_rejected += stats["unknown_key"] + stats["duplicate_key"]
    report.classify_unknown += stats["no_signal"] + stats["junk_low_confidence"]
    report.classify_missing += stats["missing"]
    return decisions


async def _persist(
    session: AsyncSession,
    decisions: list[CanonDecision],
    model: str,
    prompt_version: str = CANON_PROMPT_VERSION,
) -> tuple[int, int]:
    """把裁决写进 `title_canon`，返回 (decided 条数, junk 条数)。

    没用 PG 的 `ON CONFLICT` —— 这段要同时跑在 SQLite 的测试库上，而量级
    （几千条）完全撑得起先查再写。

    入参**允许同一个 key 出现多次**，这里按置信度留最高的那条。一个
    `series_norm_key` 会横跨两个候选块：它丢掉尾部的拉丁别名，而 `block_key`
    留着，所以《疯狂的外星人》和《疯狂的外星人 Crazy Alien》归一到同一个
    key、却分在两个块里（实测生产库 104,527 个 key 里有 621 个这样）。两个
    块各自裁一遍，`flat` 里就有两条同 key 的裁决 —— 若这个 key 在
    `title_canon` 里还没有行（新采进来的作品就是这样，`canon rebuild` 不在
    定时流水线里），`existing` 查不到，就会 `add` 两行同主键，commit 时炸
    `UniqueViolationError: pk_title_canon`。

    ## 为什么要分段 + 重试

    「先查 `existing` 再 `add`」之间有个窗口，而**同时在跑的 parse 会往
    `title_canon` 插行** —— `canon/lookup.py::pending_row` 给没见过的键造
    `pending` 行，8 个分片进程都在干这事。窗口里被插进来的那个键，这里查不到
    于是走 `add`，flush 时撞主键。

    这个竞态一直存在，只是阶段 1（归一）一轮才碰几十个键，撞上的概率可以忽略；
    阶段 2（分类）一轮送一万个键，于是必然撞 —— run 37769919480 就是这么挂的，
    报的是 `Key (norm_key)=(佳偶天成王鹤润) already exists`。

    修法是分段落库 + 撞了就重查重试，有两个独立的理由：

    - **重试才治得了竞态。** 重查时那行已经在库里了，第二遍走的是更新分支。
    - **分段把损失关小。** 不分段的话一次冲突回滚掉整轮一万条裁决，那是一小时
      的 LLM 调用。分段之后最坏也只影响 500 条，而且重试几乎总能救回来。

    没用 `ON CONFLICT DO UPDATE` 一把梭：这段要同时跑在 SQLite 的测试库上，
    两种方言的 `on_conflict_do_update` 得分别构造，为一个每轮撞一两次的竞态
    养两条落库代码路径不值得。
    """
    if not decisions:
        return 0, 0

    best: dict[str, CanonDecision] = {}
    for decision in decisions:
        incumbent = best.get(decision.key)
        # `confidence` 是 `float | None`（模型没给、或给了个非数字时就是 None），
        # 直接比会 `TypeError: '>' not supported between ... NoneType`。
        if incumbent is None or (decision.confidence or 0.0) > (incumbent.confidence or 0.0):
            best[decision.key] = decision
    decisions = list(best.values())

    decided = junk = 0
    for start in range(0, len(decisions), PERSIST_CHUNK_SIZE):
        chunk = decisions[start : start + PERSIST_CHUNK_SIZE]
        for attempt in range(PERSIST_CONFLICT_ATTEMPTS):
            try:
                chunk_junk = await _persist_chunk(session, chunk, model, prompt_version)
            except IntegrityError:
                # 回滚才能让 session 重新可用；下一遍的 `existing` 会看见
                # 那行，走更新分支。
                await session.rollback()
                if attempt == PERSIST_CONFLICT_ATTEMPTS - 1:
                    # 不往上抛：抛了就把这一轮**已经提交**的段之外的全部
                    # 白烧掉。这一段的键留在原状（没有行、或还是 pending），
                    # 下一轮 `resolve` 会重新捞到它们。
                    logger.warning(f"落库撞车 {len(chunk)} 条，重试 {attempt + 1} 次仍冲突，跳过")
                    break
                logger.info(f"落库撞车，重查重跑这一段 {len(chunk)} 条（第 {attempt + 1} 次）")
                continue
            decided += len(chunk)
            junk += chunk_junk
            break

    return decided, junk


async def _persist_chunk(
    session: AsyncSession,
    chunk: list[CanonDecision],
    model: str,
    prompt_version: str,
) -> int:
    """落一段裁决并提交，返回这一段里 junk 的条数。调用方负责重试，见 `_persist`。"""
    existing = {
        row.norm_key: row
        for row in await session.scalars(
            select(TitleCanon).where(TitleCanon.norm_key.in_([d.key for d in chunk]))
        )
    }
    now = dt.datetime.now(dt.UTC)
    junk = 0
    for decision in chunk:
        row = existing.get(decision.key)
        if row is None:
            row = TitleCanon(norm_key=decision.key)
            session.add(row)
        row.work_norm_key = decision.work_norm_key
        row.work_title = decision.work_title
        row.season = decision.season
        row.media_type = decision.media_type
        row.year = decision.year
        row.is_junk = decision.is_junk
        row.status = CanonState.DECIDED
        row.confidence = decision.confidence
        row.model = model
        row.prompt_version = prompt_version
        row.decided_at = now
        junk += int(decision.is_junk)

    await session.commit()
    return junk


async def resolve_canon(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    key: str | None = None,
    limit: int | None = None,
    classify_limit: int = 0,
    concurrency: int = DEFAULT_CONCURRENCY,
    call_interval: float = MIN_CALL_INTERVAL,
    client: LLMClient | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ResolveReport:
    """给候选块跑 LLM 裁决，结果落 `title_canon`。

    两个阶段，各打一种残局，共用同一趟全表扫描和同一个限速器：

    1. **归一**（多候选项块）：判"这些键里哪些是同一部作品"。额度 `limit`。
    2. **分类**（单候选项块）：判孤立键的类型。额度 `classify_limit`，默认
       **0 即不跑**。它不表达作品身份，所以不可能误并，见
       `validate_classifications`。

    两阶段串行，不是为了省并发 —— 是因为一个键可能同时出现在两种块里（见
    `_singles_pending`），阶段 1 先落库，阶段 2 的 `done` 才拦得住重复付费，
    也才不会让一条只有类型的裁决盖掉一条信息更全的归一裁决。

    Args:
        dry_run: 只统计块数和抽样要送的内容，**一次调用都不发**。默认开。
        key: 只处理 `block_key` 等于它的那一块。单组演练用。
        limit: 阶段 1 最多送多少个块。配合小额预算试探。
        classify_limit: 阶段 2 最多送多少**包**（一包 ≤ `MAX_ENTRIES_PER_CALL`
            个键，也就是一次调用）。0 = 不跑这个阶段。按包而不是按键算，是为
            了让它和 `MIN_CALL_INTERVAL` 直接相乘就能估出耗时 —— 每轮的预算
            本质上是时间，不是键数。
        concurrency: 并发块数（两阶段各自内部的并发）。
        call_interval: 两次调用起跑时刻的最小间隔（秒），0 表示不节流。
        client: 注入用，测试传桩，**两个阶段共用它**。默认按 funsecret 的配置
            各构造一个 —— 两阶段的 `tool_schema` 不同，而它是构造时绑定的。
        on_progress: 每个阶段落库后调一次，入参是累计已裁决的 key 数。

    可中断续跑：已经是 `decided` 的 key 会被跳过，块里全部 key 都裁决过的块
    整块跳过。所以中断后重跑只打残局，不重复付费。
    """
    report = ResolveReport(dry_run=dry_run)

    # 先沉淀再花钱：上几轮买到的作品身份能免费判掉一批键，它们就不必再进调用了。
    sediment, settled = await sediment_mod.settle_known_works(session, apply=not dry_run)
    report.settled = sediment.settled if not dry_run else sediment.matched

    blocks = await _scan_blocks(session, report)

    if key is not None:
        blocks = {b: e for b, e in blocks.items() if b == key}

    # 已经裁决过的 key 不再送。注意**不能**按 block 过滤 —— block_key 不在库里。
    done = set(
        await session.scalars(
            select(TitleCanon.norm_key).where(TitleCanon.status == CanonState.DECIDED)
        )
    )
    # dry-run 时沉淀没真的落库，手动并进来，免得报出的待送块数虚高
    done |= settled

    pending: list[tuple[str, list[CanonEntry]]] = []
    for block, entries in blocks.items():
        if len(entries) < 2:
            continue
        report.blocks_eligible += 1
        if all(e.key in done for e in entries):
            continue
        # 整块送，连已裁决的候选项一起 —— 它们是模型判断"新来的这个该不该并进去"
        # 的锚点。重复付的那点 token 换来的是一致性。
        pending.append((block, entries))

    if limit is not None:
        pending = pending[:limit]
    report.blocks_sent = len(pending)

    for _, entries in pending[:SAMPLE_LIMIT]:
        report.samples.append(" | ".join(e.key for e in entries[:6]))

    # 阶段 2 的候选集。放在 dry-run 的 return 之前算，这样 dry-run 能同时报出
    # 两个阶段各有多少活 —— 定额度之前要先看得见规模。
    singles = _singles_pending(blocks, done, report)
    packs = [
        singles[start : start + MAX_ENTRIES_PER_CALL]
        for start in range(0, len(singles), MAX_ENTRIES_PER_CALL)
    ]
    packs = packs[:classify_limit]
    report.singles_sent = sum(len(pack) for pack in packs)
    for pack in packs[:SAMPLE_LIMIT]:
        report.classify_samples.append(" | ".join(e.key for e in pack[:6]))

    if dry_run:
        return report

    gate = asyncio.Semaphore(concurrency)
    pacer = _Pacer(call_interval)

    if pending:
        resolve_client = client or OpenAICompatClient(
            tool_schema=prompts.TOOL_SCHEMA, tool_name=prompts.TOOL_NAME
        )

        async def run(entries: list[CanonEntry]) -> dict[str, CanonDecision]:
            async with gate:
                return await _resolve_block(resolve_client, entries, report, pacer)

        results = await asyncio.gather(*(run(entries) for _, entries in pending))

        # 落库在**所有**调用做完之后一次性做：`_persist` 要 commit，而 commit 会
        # 把 `_scan_blocks` 那个流式游标连根拔掉 —— 那个游标此刻已经消费完了，
        # 但一边并发调用一边 commit 还会让会话被多个任务同时碰，异步会话不是线程安全的。
        flat = [d for result in results for d in result.values()]
        decided, junk = await _persist(session, flat, resolve_client.model)
        report.decided = decided
        report.junk = junk
        if on_progress is not None:
            on_progress(decided)
        # 阶段 1 刚裁决的键不能再送进阶段 2 ——「已就位」的判断只看 `done`。
        done |= {d.key for d in flat}
        packs = [[e for e in pack if e.key not in done] for pack in packs]
        packs = [pack for pack in packs if pack]
        report.singles_sent = sum(len(pack) for pack in packs)

    if packs:
        classify_client = client or OpenAICompatClient(
            tool_schema=prompts.CLASSIFY_TOOL_SCHEMA, tool_name=prompts.CLASSIFY_TOOL_NAME
        )

        async def classify(pack: list[CanonEntry]) -> list[CanonDecision]:
            async with gate:
                return await _classify_pack(classify_client, pack, report, pacer)

        packed = await asyncio.gather(*(classify(pack) for pack in packs))

        typed = [d for result in packed for d in result]
        classified, junk = await _persist(
            session, typed, classify_client.model, CANON_CLASSIFY_PROMPT_VERSION
        )
        report.classified = classified
        report.classify_junk = junk
        if on_progress is not None:
            on_progress(report.decided + classified)

    return report
