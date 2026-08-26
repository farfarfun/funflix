"""夸克网盘匿名探针。

走分享页的 token 接口，不需要登录。实测响应（2026-08）：

- 有效：`{"status":200,"code":0,"data":{"stoken":...,"title":...,"expired_type":1,...}}`
- 失效：`{"status":404,"code":41006,"message":"分享不存在"}`

**这是逆向出来的私有接口，会随网盘改版失效。**
所以 `classify` 判不出来时返回 `None`，由骨架归到 ERROR 而不是 INVALID ——
探针挂了就把整库资源标成失效，是这类系统最容易犯也最难发现的错误。
"""

from __future__ import annotations

from typing import Any

from funflix.base.enums import CheckStatus, Provider
from funflix.services.verify.base import AnonymousHttpProbe, CheckOutcome, LinkRef

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


def classify(payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
    """把接口响应翻译成校验结论。抽成纯函数以便离线测试。

    返回 `None` 表示"这个响应看不懂"，交给骨架归到 ERROR。
    这里**绝不**自己造 INVALID 兜底 —— 接口改版时那会误杀整库。
    """
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

    return None


class QuarkProbe(AnonymousHttpProbe):
    name = "quark-anon-v1"
    provider = Provider.QUARK

    endpoint = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token"
    params = {"pr": "ucpro", "fr": "pc"}
    referer = "https://pan.quark.cn/"

    def build_payload(self, ref: LinkRef) -> dict[str, Any]:
        return {"pwd_id": ref.share_id, "passcode": ref.passcode or ""}

    def classify(self, payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
        return classify(payload, http_code)
