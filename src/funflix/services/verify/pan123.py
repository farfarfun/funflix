"""123 云盘匿名探针。"""

from __future__ import annotations

from typing import Any

from funflix.base.enums import CheckStatus, Provider
from funflix.services.verify.base import AnonymousHttpProbe, CheckOutcome, LinkRef

_BASE62 = "Tvd3hHA9QEkom14xpfaBJIMwgFYGPXn2sWCNORDr80KuUSl7bZcetizL5q6yVj"
_GONE_HINTS = ("不存在", "已失效", "已过期", "已取消", "已删除", "违规")
_PASSWORD_HINTS = ("提取码", "密码")


def _owner_id(share_id: str) -> str | None:
    value = 0
    for power, char in enumerate(share_id.partition("-")[0]):
        digit = _BASE62.find(char)
        if digit < 0:
            return None
        value += digit * 62**power
    return str(value) if value > 0 else None


def classify(payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
    """把 123 云盘分享详情接口的响应翻译成校验结论。

    `code == 0` 时看 `data`：`Expired` 为真判 INVALID，否则判 VALID 并带上
    首个文件的标题/大小。`code != 0` 时退化成看 `message` 文案：命中
    `_GONE_HINTS`（不存在/已失效等）判 INVALID，命中 `_PASSWORD_HINTS`
    （提取码/密码）判 NEED_PASSWORD。其余情况看不懂，返回 `None` 交给骨架
    归到 ERROR。

    Args:
        payload: 接口返回的 JSON 响应体。
        http_code: HTTP 状态码，原样透传进结论。

    Returns:
        解析出的结论；无法识别的响应返回 `None`。
    """
    code = payload.get("code")
    message = str(payload.get("message") or "")

    if code == 0:
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        if data.get("Expired") is True:
            return CheckOutcome(CheckStatus.INVALID, http_code, "分享已过期")
        items = data.get("InfoList")
        first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        size = first.get("Size")
        return CheckOutcome(
            CheckStatus.VALID,
            http_code,
            detail=f"items={data.get('Len', len(items) if isinstance(items, list) else 0)}",
            title=first.get("FileName") or None,
            size_bytes=size if isinstance(size, int) and size >= 0 else None,
        )

    if any(hint in message for hint in _GONE_HINTS):
        return CheckOutcome(CheckStatus.INVALID, http_code, message)
    if any(hint in message for hint in _PASSWORD_HINTS):
        return CheckOutcome(CheckStatus.NEED_PASSWORD, http_code, message)
    return None


class Pan123Probe(AnonymousHttpProbe):
    """123 云盘匿名探针：GET 分享详情接口判断分享是否可用。

    额外从 `share_id` 反解出分享者 ID（`_owner_id`，对分享码前缀做 base62
    解码），回填到结论的 `sharer_id` 字段。
    """

    name = "pan123-anon-v1"
    provider = Provider.PAN123
    endpoint = "https://www.123pan.cn/b/api/share/get"
    referer = "https://www.123pan.cn/"
    method = "GET"

    def build_params(self, ref: LinkRef) -> dict[str, str]:
        """拼接分享详情接口的查询参数，提取码取 `ref.passcode`。

        Args:
            ref: 待校验的链接引用。

        Returns:
            请求查询参数字典。
        """
        return {
            "limit": "1",
            "next": "1",
            "orderBy": "share_id",
            "orderDirection": "desc",
            "shareKey": ref.share_id,
            "SharePwd": ref.passcode or "",
            "ParentFileId": "0",
            "Page": "1",
        }

    def classify(self, payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
        """委托给模块级 `classify` 函数解析响应。"""
        return classify(payload, http_code)

    async def check(self, ref: LinkRef) -> CheckOutcome:
        """在基类探测流程之上，补上从 `share_id` 反解出的分享者 ID。

        Args:
            ref: 待校验的链接引用。

        Returns:
            探测结论；判定为 VALID 或 NEED_PASSWORD 时附带 `sharer_id`。
        """
        outcome = await super().check(ref)
        if outcome.status in {CheckStatus.VALID, CheckStatus.NEED_PASSWORD}:
            outcome.sharer_id = _owner_id(ref.share_id)
        return outcome
