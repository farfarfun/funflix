"""城通网盘匿名探针。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from funflix.base.enums import CheckStatus, Provider
from funflix.services.text.normalize import extract_size_bytes
from funflix.services.verify.base import AnonymousHttpProbe, CheckOutcome, LinkRef

_API = "https://webapi.ctfile.com"
_GONE_CODES = {404, 503}
_INCOMPLETE_HINTS = ("not fully opened", "链接不完整", "分享不完整")


def _message(payload: dict[str, Any]) -> str:
    file = payload.get("file")
    return str(
        (file.get("message") if isinstance(file, dict) else None) or payload.get("message") or ""
    )


def classify(payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
    """把城通网盘 `getfile.php`/`getdir.php` 的响应翻译成校验结论。

    `code == 200` 且带有 `file_id`/`url` 判 VALID；`code == 423` 判
    NEED_PASSWORD；`code` 属于 `_GONE_CODES`（404/503），或 `code == 403`
    且文案命中"链接不完整"一类提示，判 INVALID；`code == 429` 判
    RATE_LIMITED。其余情况看不懂，返回 `None` 交给骨架归到 ERROR。

    Args:
        payload: 接口返回的 JSON 响应体。
        http_code: HTTP 状态码，原样透传进结论。

    Returns:
        解析出的结论；无法识别的响应返回 `None`。
    """
    code = payload.get("code")
    file = payload.get("file")
    message = _message(payload)

    if code == 200 and isinstance(file, dict) and (file.get("file_id") or file.get("url")):
        userid = file.get("userid")
        return CheckOutcome(
            CheckStatus.VALID,
            http_code,
            detail=message or None,
            title=file.get("file_name") or file.get("folder_name") or None,
            size_bytes=extract_size_bytes(str(file.get("file_size") or "")),
            sharer_id=str(userid) if userid is not None else None,
            sharer_name=file.get("username") or None,
        )
    if code == 423:
        return CheckOutcome(CheckStatus.NEED_PASSWORD, http_code, message or "需要提取码")
    if code in _GONE_CODES or (code == 403 and any(h in message for h in _INCOMPLETE_HINTS)):
        return CheckOutcome(CheckStatus.INVALID, http_code, message)
    if code == 429:
        return CheckOutcome(CheckStatus.RATE_LIMITED, http_code, message)
    return None


class CTFileProbe(AnonymousHttpProbe):
    """城通网盘匿名探针：按分享标识前缀路由到 `getfile.php` 或 `getdir.php`。

    `ref.share_id` 形如 `f/xxxx`（单文件）或 `d/xxxx`（文件夹），前缀决定
    请求哪个接口以及参数怎么拼；提取码优先取 `ref.passcode`，取不到则从
    分享 URL 的查询串里取 `p` 参数兜底。
    """

    name = "ctfile-anon-v1"
    provider = Provider.CTFILE
    referer = "https://ctfile.com/"
    method = "GET"

    def build_url(self, ref: LinkRef) -> str:
        """根据分享标识前缀（f=文件、d=文件夹）选择接口地址。

        Args:
            ref: 待校验的链接引用，`share_id` 形如 `f/xxxx` 或 `d/xxxx`。

        Returns:
            `getfile.php` 或 `getdir.php` 的完整地址。

        Raises:
            ValueError: 分享标识的前缀既不是 `f` 也不是 `d`。
        """
        route = ref.share_id.partition("/")[0].lower()
        if route.startswith("f"):
            return f"{_API}/getfile.php"
        if route.startswith("d"):
            return f"{_API}/getdir.php"
        raise ValueError(f"无法识别城通分享类型：{route}")

    def build_params(self, ref: LinkRef) -> dict[str, str]:
        """拼接查询参数；文件夹类型额外带上 `d`/`folder_id`/`fk`。

        提取码优先用 `ref.passcode`，取不到则从 `ref.url` 的查询串里取 `p`
        参数兜底。

        Args:
            ref: 待校验的链接引用。

        Returns:
            请求查询参数字典。

        Raises:
            ValueError: 分享标识缺少“路由/真实 ID”之间的路径分隔符。
        """
        route, separator, share_id = ref.share_id.partition("/")
        if not separator or not share_id:
            raise ValueError("城通分享标识缺少路径")
        query = parse_qs(urlsplit(ref.url).query)
        params = {
            "path": route,
            "passcode": ref.passcode or query.get("p", [""])[0],
            "r": "0",
            "ref": "",
            "url": ref.url,
        }
        if route.lower().startswith("d"):
            params.update(
                d=share_id,
                folder_id=query.get("d", [""])[0],
                fk=query.get("fk", [""])[0],
            )
        else:
            params["f"] = share_id
        return params

    def classify(self, payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
        """委托给模块级 `classify` 函数解析响应。"""
        return classify(payload, http_code)
