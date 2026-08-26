"""阿里云盘匿名探针。

走 `get_share_by_anonymous`，不需要登录。实测响应（2026-08）：

- 失效：HTTP 404 + `{"code":"NotFound.ShareLink"}`
- 有效：HTTP 200，返回 share_name / file_infos / expiration 等

与夸克探针同样的原则：`classify` 判不出来返回 `None`，由骨架归到 ERROR。
"""

from __future__ import annotations

from typing import Any

from funflix.base.enums import CheckStatus, Provider
from funflix.services.verify.base import AnonymousHttpProbe, CheckOutcome, LinkRef

#: 明确表示"分享没了"的错误码
_GONE_CODES = {
    "NotFound.ShareLink",
    "ShareLink.Cancelled",
    "ShareLink.Expired",
    "ForbiddenShareLinkViolation",
}
_RATE_CODES = {"TooManyRequests", "Throttling"}


def classify(payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
    """把接口响应翻译成校验结论。看不懂返回 None，交给骨架归 ERROR。"""
    code = payload.get("code")

    if not code:
        # 没有错误码就是正常返回
        if payload.get("has_pwd"):
            return CheckOutcome(
                status=CheckStatus.NEED_PASSWORD,
                http_code=http_code,
                title=payload.get("share_name") or None,
                detail="分享需要提取码",
            )
        return CheckOutcome(
            status=CheckStatus.VALID,
            http_code=http_code,
            title=payload.get("share_name") or None,
            detail=f"expiration={payload.get('expiration')}",
        )

    if code in _GONE_CODES:
        return CheckOutcome(status=CheckStatus.INVALID, http_code=http_code, detail=str(code))
    if code in _RATE_CODES:
        return CheckOutcome(status=CheckStatus.RATE_LIMITED, http_code=http_code, detail=str(code))

    return None


class AlipanProbe(AnonymousHttpProbe):
    name = "alipan-anon-v1"
    provider = Provider.ALIPAN

    endpoint = "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous"
    referer = "https://www.alipan.com/"

    def build_payload(self, ref: LinkRef) -> dict[str, Any]:
        return {"share_id": ref.share_id}

    def classify(self, payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
        return classify(payload, http_code)
