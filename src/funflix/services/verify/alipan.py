"""阿里云盘匿名探针。

走 `get_share_by_anonymous`，不需要登录。实测响应（2026-08）：

- 失效：HTTP 404 + `{"code":"NotFound.ShareLink"}`
- 有效：HTTP 200，返回 share_name / file_infos / expiration 等

与夸克探针同样的原则：判不出来归 ERROR 而不是 INVALID。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from funflix.base.enums import CheckStatus, Provider
from funflix.services.verify.base import CheckOutcome, LinkRef

logger = logging.getLogger(__name__)

_API = "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: 明确表示"分享没了"的错误码
_GONE_CODES = {
    "NotFound.ShareLink",
    "ShareLink.Cancelled",
    "ShareLink.Expired",
    "ForbiddenShareLinkViolation",
}
_RATE_CODES = {"TooManyRequests", "Throttling"}


def classify(payload: dict[str, Any], http_code: int) -> CheckOutcome:
    """把接口响应翻译成校验结论。"""
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

    logger.warning("阿里云盘返回了未知错误码 %r", code)
    return CheckOutcome(status=CheckStatus.ERROR, http_code=http_code, detail=f"未知错误码 {code}")


class AlipanProbe:
    name = "alipan-anon-v1"
    provider = Provider.ALIPAN
    needs_auth = False

    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 15.0) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    async def check(self, ref: LinkRef) -> CheckOutcome:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        started = time.monotonic()
        try:
            response = await client.post(
                _API,
                json={"share_id": ref.share_id},
                headers={
                    "User-Agent": _UA,
                    "Content-Type": "application/json",
                    "Referer": "https://www.alipan.com/",
                },
            )
            try:
                payload = response.json()
            except ValueError:
                return CheckOutcome(
                    status=CheckStatus.ERROR,
                    http_code=response.status_code,
                    detail=f"响应不是 JSON：{response.text[:120]!r}",
                )
            outcome = classify(payload, response.status_code)
        except httpx.HTTPError as exc:
            outcome = CheckOutcome(status=CheckStatus.ERROR, detail=f"{type(exc).__name__}: {exc}")
        finally:
            if self._owns_client:
                await client.aclose()

        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        return outcome
