"""入库时查 `title_canon`，决定一个抽取项该落到哪个 Work / 哪一季。

这是**防回退**的那一环。阶段 1~4 把历史数据并好之后，新数据如果还按老规则
各自建行，并好的东西会被重新拆开 —— 所以 `_upsert_media` 必须先问一句
「这个键我们已经裁决过了吗」。

裁决落在 `title_canon` 上，三种状态三种待遇：

- `decided` —— 直接用裁决的作品键、季、类型、年份。这是 LLM（或人工）
  花过钱得出的结论，比规则可信。
- `pending` / `rejected` —— 还没裁决，或裁决被校验挡下了。按规则给一个
  临时归属，等下一轮 `canon resolve` 再说。
- 查不到 —— 按规则给临时归属，**并顺手写一行 `pending`**，这样
  `canon resolve` 下次跑就能把这个新键捞进去。

## `season` 又一次：`None` 不是「第 0 季」

`title_canon.season` 回答的是「这个键本身锁定了哪一季」。是数字就覆盖规则的
逐行判定；是 `None` 就**不覆盖**，用 `extract_season` 对这一行的判定。
细节见 `canon/apply.py` 的模块说明，这里的语义必须和它一致 —— 否则同一条
裁决在「历史数据补救」和「新数据入库」两条路上会产生不同结果。

## 为什么是纯函数

真正的 IO（查 `title_canon`、get-or-create Work / Media）留在 `runner.py`,
那里有批缓存和 flush 时机的讲究。这里只做决策，于是能脱开数据库单测 ——
而决策错一次就是一个虚假作品。
"""

from __future__ import annotations

from dataclasses import dataclass

from funflix.base.enums import MediaType
from funflix.models.canon import CanonState, TitleCanon
from funflix.models.media import NO_SEASON, UNKNOWN_YEAR
from funflix.services.text.normalize import extract_season, series_norm_key


@dataclass(frozen=True, slots=True)
class CanonTarget:
    """一个抽取项的归属裁决结果。

    `is_junk` 为真时其余字段都没有意义 —— 调用方该把这一项整个丢掉，
    链接转成未归属资源（和抽取器丢弃目录页的处理一致）。
    """

    work_norm_key: str
    work_title: str
    season: int
    media_type: MediaType
    year: int
    is_junk: bool = False
    #: 为真表示 `title_canon` 里还没有这个键，调用方应补一行 `pending`。
    needs_pending_row: bool = False


def _fallback(title: str, media_type: MediaType, year: int) -> CanonTarget:
    """没有可用裁决时按规则给的临时归属。

    和 `canon rebuild` 用的是同一对函数（`series_norm_key` / `extract_season`），
    所以「新数据入库」和「历史数据重算」会把同一个标题放到同一个地方。
    两边各写一套规则是这类改造最常见的退化来源。
    """
    return CanonTarget(
        work_norm_key=series_norm_key(title),
        work_title=title,
        season=extract_season(title) or NO_SEASON,
        media_type=media_type,
        year=year,
        needs_pending_row=True,
    )


def resolve_target(
    *,
    title: str,
    media_type: MediaType,
    year: int | None,
    canon: TitleCanon | None,
) -> CanonTarget:
    """决定这个抽取项该挂到哪个 Work 的哪一季。

    Args:
        title: 抽取器给出的清洗后标题。
        media_type: 抽取器判出的类型，可能是 `UNKNOWN`。
        year: 抽取器判出的年份，`None` 当作未知。
        canon: 按 `item.norm_key` 查到的裁决行，查不到传 `None`。

    类型和年份的取舍是「宁缺毋滥」：裁决里是 unknown / 0 的时候用这一项
    自己判出来的值补上，反过来**不**用这一项的值去覆盖裁决里已有的值 ——
    裁决是看完整组标题之后给的，单条分享的判断没它可信。
    """
    item_year = UNKNOWN_YEAR if year is None else year

    if canon is None:
        return _fallback(title, media_type, item_year)

    # `status` 是普通字符串列（见 `CanonState` 的说明），**必须用 `==`** ——
    # 从库里读回来的是新字符串对象，`is` 比较会偶发地为假。
    if canon.status != CanonState.DECIDED:
        # 已经有 pending 行了，不要再写一行。
        return CanonTarget(
            work_norm_key=series_norm_key(title),
            work_title=title,
            season=extract_season(title) or NO_SEASON,
            media_type=media_type,
            year=item_year,
        )

    if canon.is_junk:
        return CanonTarget(
            work_norm_key="",
            work_title="",
            season=NO_SEASON,
            media_type=media_type,
            year=item_year,
            is_junk=True,
        )

    work_title = canon.work_title or title
    work_key = canon.work_norm_key or series_norm_key(work_title)
    if not work_key:
        # 裁决行坏了（标题洗完是空的）—— 退回规则，别把这一项挂到一个
        # 空键的 Work 上，那会变成一个吸收所有脏数据的黑洞。
        return _fallback(title, media_type, item_year)

    return CanonTarget(
        work_norm_key=work_key,
        work_title=work_title,
        # `None` = 这个键没锁定季，用这一行自己的判定。
        season=canon.season if canon.season is not None else (extract_season(title) or NO_SEASON),
        media_type=(canon.media_type if canon.media_type is not MediaType.UNKNOWN else media_type),
        year=canon.year if canon.year != UNKNOWN_YEAR else item_year,
    )


def pending_row(norm_key: str, target: CanonTarget) -> TitleCanon:
    """为一个没见过的键造一行 `pending`，供 `canon resolve` 下次捞走。

    带上规则算出的临时值：万一 LLM 那一路始终没跑，这行至少还能当作
    「规则的结论」被读出来，而不是一行空壳。

    `season` 留 `None` 而不是写 `target.season`：这一列的语义是「**这个键**
    锁定了哪一季」，而 `target.season` 是规则对**某一条分享**的判定。
    把后者写进来就等于拿一条分享的判断去锁住整个键 —— `大主宰` 这个通名键
    会被第一条碰巧带 `第2季` 的分享永久钉在第 2 季上。
    """
    return TitleCanon(
        norm_key=norm_key,
        work_norm_key=target.work_norm_key,
        work_title=target.work_title[:500],
        season=None,
        media_type=target.media_type,
        year=target.year,
        is_junk=False,
        status=CanonState.PENDING,
    )
