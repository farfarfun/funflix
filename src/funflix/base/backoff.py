"""失败退避曲线。

采集 / 解析 / 校验三条流水线用的是同一条曲线：`min(60s * 2**n, 6h)`。
在这里统一，是因为三处各自复制过一份 —— 退避参数一旦悄悄分叉，
"为什么这一层重试得特别猛"会变成很难查的问题。
"""

from __future__ import annotations

from datetime import timedelta

#: 首次失败后的基准等待时长，之后逐次翻倍。
BASE_BACKOFF = timedelta(seconds=60)

#: 退避上限。再长就不如等人工介入了。
MAX_BACKOFF = timedelta(hours=6)

#: 指数的封顶值。60s * 2**20 已是 728 天，远超 MAX_BACKOFF，再大没有意义。
#:
#: 封顶不是为了省算力，而是防溢出：`source.consecutive_failures` 没有上限，
#: 一个持续失败的源攒到 50 次时，`timedelta(seconds=60 * 2**50)` 会直接
#: 抛 OverflowError（timedelta 上限约 10 亿天），把采集循环打断。
_MAX_EXPONENT = 20


def backoff(attempts: int) -> timedelta:
    """第 `attempts` 次失败后应该等多久再试。

    `attempts` 从 1 起算：第一次失败传 1，得 2 分钟。

    传 0 或负数返回 `BASE_BACKOFF` 而不是 0 —— 立刻重试一个刚刚失败的任务
    只会同样地再失败一次，白白多打一次外部接口。
    """
    if attempts <= 0:
        return BASE_BACKOFF
    return min(BASE_BACKOFF * 2 ** min(attempts, _MAX_EXPONENT), MAX_BACKOFF)
