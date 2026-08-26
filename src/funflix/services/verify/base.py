"""校验环节的抽象。

一个 LinkProbe 判断「某条网盘分享链接现在还能不能用」。

设计原则：**能匿名探测就绝不登录**。匿名探针无凭证依赖、无账号风险、
可高并发；只有匿名判不出来时才降级到带登录态的驱动（fundrive）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from funflix.base.enums import CheckStatus, Provider


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
