"""HTTP 请求的公共约定。

采集器和探针打的都是别人的网页与私有接口，请求头得像个真浏览器。
User-Agent 曾经在五个文件里各抄了一份 —— 等哪天这个版本号旧到被风控盯上，
那就是五处一起改，漏一处就只有那一个源在莫名其妙地失败。
"""

from __future__ import annotations

#: 统一的浏览器 User-Agent。
#:
#: 定期跟一下主流 Chrome 版本：太旧会被当成爬虫。改这里一处即可全局生效。
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def json_headers(referer: str) -> dict[str, str]:
    """打 JSON 私有接口用的请求头。

    `Referer` 必须给对：这些接口大多会校验来源页，缺了会被直接拒掉，
    而拒掉的响应看起来跟"分享失效"很像 —— 正是最该避免的误判。
    """
    return {
        "User-Agent": DEFAULT_UA,
        "Content-Type": "application/json",
        "Referer": referer,
    }
