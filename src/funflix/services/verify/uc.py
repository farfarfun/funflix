"""UC 网盘匿名探针。

UC 与夸克同属一家，分享接口是**同一套**，连业务码都一样。
实测（2026-08，`pc-api.uc.cn`，假分享 ID）：

    {"status":404,"code":41006,"message":"分享不存在"}

`41006` 正是夸克那边的"分享没了"码，所以这里直接复用
`quark.classify`，不另抄一份码表 —— 抄一份就意味着夸克那边加了新码之后，
UC 这边还在把它当"未知响应"。

两处差别：
- 域名走 `pc-api.uc.cn`。`drive.uc.cn/1/...` 直接返回 403 Forbidden 的 HTML，
  不是接口。
- 渠道参数 `pr=UCBrowser`（夸克是 `ucpro`）。
"""

from __future__ import annotations

from typing import Any

from funflix.base.enums import Provider
from funflix.services.verify.base import AnonymousHttpProbe, CheckOutcome, LinkRef
from funflix.services.verify.quark import classify as quark_classify


class UCProbe(AnonymousHttpProbe):
    name = "uc-anon-v1"
    provider = Provider.UC

    endpoint = "https://pc-api.uc.cn/1/clouddrive/share/sharepage/token"
    params = {"pr": "UCBrowser", "fr": "pc"}
    referer = "https://drive.uc.cn/"

    def build_payload(self, ref: LinkRef) -> dict[str, Any]:
        return {"pwd_id": ref.share_id, "passcode": ref.passcode or ""}

    def classify(self, payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
        return quark_classify(payload, http_code)
