"""夸克网盘匿名探针。

走分享页的 token 接口，不需要登录。实测响应（2026-08）：

- 有效：`{"status":200,"code":0,"data":{"stoken":...,"title":...,"expired_type":1,...}}`
- 失效：`{"status":404,"code":41006,"message":"分享不存在"}`
- 失效：`{"status":404,"code":41004,"message":"文件不存在"}`（分享还在、里面的文件被删了）
- 服务端抽风：`{"status":500,"code":15000,"message":"inner error, requestId ..."}`
  —— 刻意**不**收进任何码表，它该走 ERROR 退避重试（生产库里只有 88 条）。

**这是逆向出来的私有接口，会随网盘改版失效。**
所以 `classify` 判不出来时返回 `None`，由骨架归到 ERROR 而不是 INVALID ——
探针挂了就把整库资源标成失效，是这类系统最容易犯也最难发现的错误。
"""

from __future__ import annotations

from typing import Any

from funflix.base.enums import CheckStatus, Provider
from funflix.services.verify.base import AnonymousHttpProbe, CheckOutcome, LinkRef

#: 明确表示"这个分享没了"的业务码。
#:
#: `41004 文件不存在` 是生产库里**第二多**的失效码（`link_check` 里 57,697 条），
#: 此前不在表上、落到 ERROR —— 而 ERROR 的语义是「判不出来，排退避重试」，
#: 于是这 5.7 万条链接每轮都被重新探一遍，永远探不出结论，还挤掉了真正待校验
#: 链接的名额。它和 `41006 分享不存在` 的区别只是夸克那边分享还在、分享里的
#: 文件被删了，对使用者是一样的：点进去拿不到东西。
_GONE_CODES = {41004, 41006, 41007, 41008, 41031}
#: 明确表示"要提取码"的业务码
_NEED_PASSWORD_CODES = {41005}
#: 被限流 / 风控
_RATE_LIMITED_CODES = {40001, 41013, 429}

#: 业务码判不出来时，用返回文案兜底
_GONE_HINTS = ("分享不存在", "文件不存在", "已失效", "已删除", "已取消", "违规", "过期")
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
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        return CheckOutcome(
            status=CheckStatus.VALID,
            http_code=http_code,
            title=data.get("title") or None,
            detail=f"expired_type={data.get('expired_type')}",
            sharer_name=author.get("nick_name") or None,
            sharer_avatar_url=author.get("avatar_url") or None,
        )

    if code in _NEED_PASSWORD_CODES or any(h in message for h in _PASSWORD_HINTS):
        return CheckOutcome(status=CheckStatus.NEED_PASSWORD, http_code=http_code, detail=message)

    if code in _GONE_CODES or any(h in message for h in _GONE_HINTS):
        return CheckOutcome(status=CheckStatus.INVALID, http_code=http_code, detail=message)

    if code in _RATE_LIMITED_CODES or any(h in message for h in _RATE_HINTS):
        return CheckOutcome(status=CheckStatus.RATE_LIMITED, http_code=http_code, detail=message)

    return None


class QuarkProbe(AnonymousHttpProbe):
    """夸克网盘匿名探针：POST 分享页 token 接口判断分享是否可用。

    这是逆向出来的私有接口，判定逻辑见模块级 `classify` 函数；UC 网盘共用
    同一套接口和业务码，直接复用这里的 `classify`（见 `uc.py`）。
    """

    name = "quark-anon-v1"
    provider = Provider.QUARK

    endpoint = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token"
    params = {"pr": "ucpro", "fr": "pc"}
    referer = "https://pan.quark.cn/"

    def build_payload(self, ref: LinkRef) -> dict[str, Any]:
        """构造探测请求体：分享 ID 与提取码。

        Args:
            ref: 待校验的链接引用。

        Returns:
            包含 `pwd_id`、`passcode` 的请求体。
        """
        return {"pwd_id": ref.share_id, "passcode": ref.passcode or ""}

    def classify(self, payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
        """委托给模块级 `classify` 函数解析响应。"""
        return classify(payload, http_code)
