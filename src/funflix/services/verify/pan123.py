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
    name = "pan123-anon-v1"
    provider = Provider.PAN123
    endpoint = "https://www.123pan.cn/b/api/share/get"
    referer = "https://www.123pan.cn/"
    method = "GET"

    def build_params(self, ref: LinkRef) -> dict[str, str]:
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
        return classify(payload, http_code)

    async def check(self, ref: LinkRef) -> CheckOutcome:
        outcome = await super().check(ref)
        if outcome.status in {CheckStatus.VALID, CheckStatus.NEED_PASSWORD}:
            outcome.sharer_id = _owner_id(ref.share_id)
        return outcome
