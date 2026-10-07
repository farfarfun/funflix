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

**只送 ≥2 个候选项的块**（实测 4,899 个）。单候选项的块没有可并的对象，
送过去纯烧钱；它们的季号/类型/年份由规则兜着，等真有需要再单独扫一遍。

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
from sqlalchemy.ext.asyncio import AsyncSession

from funflix.base.enums import MediaType
from funflix.models import Media
from funflix.models.canon import CANON_PROMPT_VERSION, CanonState, TitleCanon
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
    looks_like_junk_title,
    series_norm_key,
)

logger = getLogger("funflix")

#: 一次调用最多塞多少个候选项。再多模型开始漏项、`key` 照抄也开始出错。
MAX_ENTRIES_PER_CALL = 50

#: 并发调用数。受网关限流约束，不是 CPU 约束。
DEFAULT_CONCURRENCY = 8

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


async def _scan_blocks(session: AsyncSession, report: ResolveReport) -> dict[str, list[CanonEntry]]:
    """扫全表，按 `block_key` → `series_norm_key` 聚合。

    只读，一趟流式扫完。跳过规则已经判成垃圾的行 —— 它们在阶段 1 就该被删掉，
    这里再拦一道是为了让 resolve 在没跑过 purge 的库上也不会把垃圾送去烧钱。
    """
    blocks: dict[str, dict[str, CanonEntry]] = {}
    rows = await session.stream(select(Media.title, Media.resource_count))
    async for title, resource_count in rows:
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


async def _resolve_block(
    client: LLMClient,
    entries: list[CanonEntry],
    report: ResolveReport,
) -> dict[str, CanonDecision]:
    """裁决一个候选块，按需拆页。页之间顺序跑，后面的页能看到前面的结论。"""
    decided: dict[str, CanonDecision] = {}
    for start in range(0, len(entries), MAX_ENTRIES_PER_CALL):
        page = entries[start : start + MAX_ENTRIES_PER_CALL]
        lines = [prompts.format_entry(e.key, e.rows, e.resources, e.sample) for e in page]
        user = prompts.build_user_message(lines) + _decided_context(decided)

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


async def _persist(
    session: AsyncSession, decisions: list[CanonDecision], model: str
) -> tuple[int, int]:
    """把裁决写进 `title_canon`，返回 (decided 条数, junk 条数)。

    没用 PG 的 `ON CONFLICT` —— 这段要同时跑在 SQLite 的测试库上，而量级
    （几千条）完全撑得起先查再写。
    """
    if not decisions:
        return 0, 0

    keys = [d.key for d in decisions]
    existing = {
        row.norm_key: row
        for row in await session.scalars(select(TitleCanon).where(TitleCanon.norm_key.in_(keys)))
    }
    now = dt.datetime.now(dt.UTC)
    junk = 0
    for decision in decisions:
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
        row.prompt_version = CANON_PROMPT_VERSION
        row.decided_at = now
        junk += int(decision.is_junk)

    await session.commit()
    return len(decisions), junk


async def resolve_canon(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    key: str | None = None,
    limit: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    client: LLMClient | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ResolveReport:
    """给候选块跑 LLM 裁决，结果落 `title_canon`。

    Args:
        dry_run: 只统计块数和抽样要送的内容，**一次调用都不发**。默认开。
        key: 只处理 `block_key` 等于它的那一块。单组演练用。
        limit: 最多送多少个块。配合小额预算试探。
        concurrency: 并发块数。
        client: 注入用，测试传桩。默认按 funsecret 的配置构造。
        on_progress: 每做完一个块调一次，入参是累计已裁决的 key 数。

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

    if dry_run:
        return report

    if client is None:
        client = OpenAICompatClient(tool_schema=prompts.TOOL_SCHEMA, tool_name=prompts.TOOL_NAME)

    gate = asyncio.Semaphore(concurrency)

    async def run(entries: list[CanonEntry]) -> dict[str, CanonDecision]:
        async with gate:
            return await _resolve_block(client, entries, report)

    results = await asyncio.gather(*(run(entries) for _, entries in pending))

    # 落库在**所有**调用做完之后一次性做：`_persist` 要 commit，而 commit 会
    # 把 `_scan_blocks` 那个流式游标连根拔掉 —— 那个游标此刻已经消费完了，
    # 但一边并发调用一边 commit 还会让会话被多个任务同时碰，异步会话不是线程安全的。
    flat = [d for result in results for d in result.values()]
    decided, junk = await _persist(session, flat, client.model)
    report.decided = decided
    report.junk = junk
    if on_progress is not None:
        on_progress(decided)
    return report
