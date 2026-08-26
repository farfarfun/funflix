"""夸克网盘匿名探针。

走分享页的 token 接口，不需要登录。实测响应（2026-08）：

- 有效：`{"status":200,"code":0,"data":{"stoken":...,"title":...,"expired_type":1,...}}`
- 失效：`{"status":404,"code":41006,"message":"分享不存在"}`

**这是逆向出来的私有接口，会随网盘改版失效。**
所以「判不出来」一律归到 ERROR 而不是 INVALID —— 探针挂了就把整库资源
标成失效，是这类系统最容易犯也最难发现的错误。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from funflix.base.enums import CheckStatus, Provider
from funflix.services.verify.base import CheckOutcome, LinkRef

logger = logging.getLogger(__name__)

_API = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token"
_PARAMS = {"pr": "ucpro", "fr": "pc"}
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: 明确表示"这个分享没了"的业务码
_GONE_CODES = {41006, 41007, 41008}
#: 明确表示"要提取码"的业务码
_NEED_PASSWORD_CODES = {41005}
#: 被限流 / 风控
_RATE_LIMITED_CODES = {40001, 41013, 429}

#: 业务码判不出来时，用返回文案兜底
_GONE_HINTS = ("分享不存在", "已失效", "已删除", "已取消", "违规", "过期")
_PASSWORD_HINTS = ("提取码", "密码", "访问码")
_RATE_HINTS = ("频繁", "限制", "稍后")


def classify(payload: dict[str, Any], http_code: int) -> CheckOutcome:
    """把接口响应翻译成校验结论。抽成纯函数以便离线测试。"""
    code = payload.get("code")
    message = str(payload.get("message") or "")

    if code == 0:
        data = payload.get("data") or {}
        return CheckOutcome(
            status=CheckStatus.VALID,
            http_code=http_code,
            title=data.get("title") or None,
            detail=f"expired_type={data.get('expired_type')}",
        )

    if code in _NEED_PASSWORD_CODES or any(h in message for h in _PASSWORD_HINTS):
        return CheckOutcome(status=CheckStatus.NEED_PASSWORD, http_code=http_code, detail=message)

    if code in _GONE_CODES or any(h in message for h in _GONE_HINTS):
        return CheckOutcome(status=CheckStatus.INVALID, http_code=http_code, detail=message)

    if code in _RATE_LIMITED_CODES or any(h in message for h in _RATE_HINTS):
        return CheckOutcome(status=CheckStatus.RATE_LIMITED, http_code=http_code, detail=message)

    # 认不出来的响应：可能是接口改版了。归到 ERROR 而非 INVALID ——
    # 宁可下轮重试，也不要把还能用的链接误标成失效。
    logger.warning("夸克返回了未知响应 code=%s message=%r", code, message[:80])
    return CheckOutcome(
        status=CheckStatus.ERROR, http_code=http_code, detail=f"未知响应 code={code}: {message}"
    )


class QuarkProbe:
    name = "quark-anon-v1"
    provider = Provider.QUARK
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
                params=_PARAMS,
                json={"pwd_id": ref.share_id, "passcode": ref.passcode or ""},
                headers={
                    "User-Agent": _UA,
                    "Content-Type": "application/json",
                    "Referer": "https://pan.quark.cn/",
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
            # 网络问题不是关于链接的结论
            outcome = CheckOutcome(status=CheckStatus.ERROR, detail=f"{type(exc).__name__}: {exc}")
        finally:
            if self._owns_client:
                await client.aclose()

        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        return outcome
