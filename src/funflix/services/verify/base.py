"""校验环节的抽象。

一个 LinkProbe 判断「某条网盘分享链接现在还能不能用」。

设计原则：**能匿名探测就绝不登录**。匿名探针无凭证依赖、无账号风险、
可高并发；只有匿名判不出来时才降级到带登录态的驱动（fundrive）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from funflix.base.enums import CheckStatus, Provider
from funflix.base.http import json_headers

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LinkRef:
    """待校验的链接。"""

    provider: Provider
    share_id: str
    url: str
    passcode: str | None = None


@dataclass(slots=True)
class CheckOutcome:
    """一次校验的结论。"""

    status: CheckStatus
    http_code: int | None = None
    detail: str | None = None
    #: 网盘侧返回的资源名，可用于回填校正标题
    title: str | None = None
    size_bytes: int | None = None
    latency_ms: int | None = None

    @property
    def is_conclusive(self) -> bool:
        """是否得到了关于链接本身的明确结论。

        限流和探针异常都不是关于链接的结论 —— 把它们当成"失效"
        会在网盘抽风时把整库资源误杀一遍。
        """
        return self.status in {
            CheckStatus.VALID,
            CheckStatus.INVALID,
            CheckStatus.NEED_PASSWORD,
        }


@runtime_checkable
class LinkProbe(Protocol):
    """网盘探针接口。"""

    #: 探针实现标识，写进 link_check.probe。换实现后能区分历史数据。
    name: str
    provider: Provider
    #: 是否需要登录凭证。匿名探针为 False。
    needs_auth: bool

    async def check(self, ref: LinkRef) -> CheckOutcome: ...


class AnonymousHttpProbe:
    """匿名 HTTP 探针的公共骨架。

    子类只描述**这个网盘特有的部分**：打哪个地址、请求体长什么样、
    以及怎么把响应翻译成结论。请求、超时、连接生命周期、异常兜底、
    耗时统计都在这里，各探针不再各抄一遍。

    ## 为什么这层抽象是安全设施而不只是去重

    这套系统最危险的错误是**把探针自己的故障当成"链接失效"**：接口改版、
    被风控、返回一段 HTML 错误页——任何一种被判成 INVALID，都会在一轮复查里
    把整库资源误杀，而且看起来完全正常（状态是"已确认失效"，不是报错）。

    所以骨架把"判不出来"的所有路径都收在自己手里，并且让**安全的结果成为
    默认行为**：`classify` 返回 `None` 就表示"看不懂"，骨架翻译成 ERROR。
    新增探针的人即使忘了写兜底分支，拿到的也是 ERROR 而不是 INVALID ——
    忘记做某件事时得到安全结果，比要求每个人都记得写对，可靠得多。
    """

    name: str = "anonymous-http"
    provider: Provider
    needs_auth = False

    #: 私有接口地址
    endpoint: str = ""
    #: 查询串参数
    params: dict[str, str] | None = None
    #: Referer，多数接口会校验来源页
    referer: str = ""
    #: HTTP 方法。个别网盘用 GET。
    method: str = "POST"

    def __init__(self, client: Any = None, timeout: float = 15.0) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    def build_payload(self, ref: LinkRef) -> dict[str, Any]:
        """请求体。子类必须实现。"""
        raise NotImplementedError

    def classify(self, payload: dict[str, Any], http_code: int) -> CheckOutcome | None:
        """把响应翻译成结论；**看不懂就返回 None**，不要自己造 INVALID。

        子类必须实现。返回 None 时骨架会记一条 warning 并归到 ERROR。
        """
        raise NotImplementedError

    async def check(self, ref: LinkRef) -> CheckOutcome:
        import httpx

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        started = time.monotonic()
        outcome: CheckOutcome
        try:
            response = await client.request(
                self.method,
                self.endpoint,
                params=self.params,
                json=self.build_payload(ref),
                headers=json_headers(self.referer),
            )
            try:
                payload = response.json()
            except ValueError:
                # 返回的不是 JSON —— 多半是错误页或验证码页，不是关于链接的结论
                outcome = CheckOutcome(
                    status=CheckStatus.ERROR,
                    http_code=response.status_code,
                    detail=f"响应不是 JSON：{response.text[:120]!r}",
                )
            else:
                if not isinstance(payload, dict):
                    outcome = CheckOutcome(
                        status=CheckStatus.ERROR,
                        http_code=response.status_code,
                        detail=f"响应不是 JSON 对象：{type(payload).__name__}",
                    )
                else:
                    outcome = self._classify_safely(payload, response.status_code)
        except httpx.HTTPError as exc:
            # 网络问题不是关于链接的结论
            outcome = CheckOutcome(status=CheckStatus.ERROR, detail=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            # 解析逻辑自己抛了 —— 同样不能算链接失效
            logger.exception("%s 探针异常", self.name)
            outcome = CheckOutcome(status=CheckStatus.ERROR, detail=f"{type(exc).__name__}: {exc}")
        finally:
            if self._owns_client:
                await client.aclose()

        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def _classify_safely(self, payload: dict[str, Any], http_code: int) -> CheckOutcome:
        outcome = self.classify(payload, http_code)
        if outcome is not None:
            return outcome
        # 认不出来的响应：多半是接口改版了。归 ERROR 而非 INVALID ——
        # 宁可下轮重试，也不要把还能用的链接误标成失效。
        logger.warning("%s 返回了未知响应，已归为 ERROR：%s", self.name, str(payload)[:160])
        return CheckOutcome(
            status=CheckStatus.ERROR, http_code=http_code, detail=f"未知响应：{str(payload)[:160]}"
        )
