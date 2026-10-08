"""阿里云盘匿名探针。

走 `get_share_by_anonymous`，不需要登录。实测响应（2026-08）：

- 失效：HTTP 404 + `{"code":"NotFound.ShareLink"}`
- 有效：HTTP 200，返回 share_name / file_infos / expiration 等

与夸克探针同样的原则：`classify` 判不出来返回 `None`，由骨架归到 ERROR。
这个原则是为了"接口改版别把整库资源误杀"，但反过来也有代价 —— 一个**明确
表示分享没了**的业务码漏在码表外面，就会被当成"判不出结论"无限重试下去，
把阿里这个限速最紧的网盘的调用额度白烧掉。见 `_GONE_CODES` 的注释。
"""

from __future__ import annotations

from typing import Any

from funflix.base.enums import CheckStatus, Provider
from funflix.services.verify.base import AnonymousHttpProbe, CheckOutcome, LinkRef

#: 明确表示"分享没了"的错误码
#:
#: `ShareLink.Forbidden`（正文 `share_link is forbidden`）和
#: `ShareLink.ContentInvalid` 是**后补的**，补之前它们落在 `classify` 的兜底
#: `return None` 上、被骨架归成 ERROR —— 而 ERROR 的含义是"这不是关于链接的
#: 结论"，`_next_check_at` 会给它排退避重试，封顶 6 小时一次、永远重试下去。
#:
#: 代价是实测出来的：生产库里 `ShareLink.Forbidden` 攒了 **17374 条探测历史、
#: 只对应 2984 个去重 share_id**（平均每条白探 5.8 次），run 37797533687 那
#: 一轮 verify 1852 条结论里 640 条是它，占阿里全部调用的 35%。而阿里是整个
#: verify 阶段的闸门：按网盘限速 1 次/秒、被限流后还会自适应放慢（那轮放慢到
#: 7.29 秒一次），这 35% 等于直接砍掉三分之一的校验吞吐。
#:
#: 判 INVALID 不是一条单向门：`_RECHECK_TTL` 给 INVALID 排 30 天后再确认一次，
#: 连续两次（`_INVALID_CONFIRM_TIMES`）才彻底退休。真要是被风控临时封掉、
#: 后来又放开了，30 天后那一次复查能捞回来。
#:
#: 补上之后的实测（run 37817812402 对比 run 37797533687，两轮 Verify 步骤
#: 时长几乎相同，80 分 06 秒 vs 82 分 11 秒）：
#:
#: | | 补之前 | 补之后 |
#: |---|---|---|
#: | 校验条数 | 1852 | **3607** |
#: | error | 772（42%） | **70（1.9%）** |
#: | valid / invalid | 578 / 494 | 2670 / 848 |
#: | 判不出结论 | 780 | **81** |
#:
#: 同样的墙上时间出了近两倍的结论。省下来的不只是那 640 次调用：阿里被限流
#: 后间隔会自适应放大（补之前实测顶在 `_MAX_INTERVAL_FACTOR` 的 16 倍上，
#: 16 秒才探一条），少打一批注定没结论的请求，间隔自己就收回来了。
_GONE_CODES = {
    "NotFound.ShareLink",
    "ShareLink.Cancelled",
    "ShareLink.Expired",
    "ForbiddenShareLinkViolation",
    "ShareLink.Forbidden",
    "ShareLink.ContentInvalid",
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
                sharer_id=payload.get("creator_id") or None,
                sharer_name=payload.get("creator_name") or None,
                sharer_avatar_url=payload.get("avatar") or None,
            )
        return CheckOutcome(
            status=CheckStatus.VALID,
            http_code=http_code,
            title=payload.get("share_name") or None,
            detail=f"expiration={payload.get('expiration')}",
            sharer_id=payload.get("creator_id") or None,
            sharer_name=payload.get("creator_name") or None,
            sharer_avatar_url=payload.get("avatar") or None,
        )

    if code in _GONE_CODES:
        return CheckOutcome(status=CheckStatus.INVALID, http_code=http_code, detail=str(code))
    if code in _RATE_CODES:
        return CheckOutcome(status=CheckStatus.RATE_LIMITED, http_code=http_code, detail=str(code))

    return None


class AlipanProbe(AnonymousHttpProbe):
    """阿里云盘匿名探针：调用 `get_share_by_anonymous` 接口判断分享是否可用。

    免登录，直接 POST 分享详情接口；失效/需要提取码/限流的判定逻辑见
    模块级 `classify` 函数。
    """

    name = "alipan-anon-v1"
    provider = Provider.ALIPAN

    endpoint = "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous"
    referer = "https://www.alipan.com/"

    def build_payload(self, ref: LinkRef) -> dict[str, Any]:
        """构造探测请求体，只需要分享 ID。

        Args:
            ref: 待校验的链接引用。

        Returns:
            包含 `share_id` 的请求体。
        """
        return {"share_id": ref.share_id}

    def classify(self, payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
        """委托给模块级 `classify` 函数解析响应。"""
        return classify(payload, http_code)
