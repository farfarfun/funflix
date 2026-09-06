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
    name = "ctfile-anon-v1"
    provider = Provider.CTFILE
    referer = "https://ctfile.com/"
    method = "GET"

    def build_url(self, ref: LinkRef) -> str:
        route = ref.share_id.partition("/")[0].lower()
        if route.startswith("f"):
            return f"{_API}/getfile.php"
        if route.startswith("d"):
            return f"{_API}/getdir.php"
        raise ValueError(f"无法识别城通分享类型：{route}")

    def build_params(self, ref: LinkRef) -> dict[str, str]:
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
        return classify(payload, http_code)
