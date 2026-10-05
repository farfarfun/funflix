"""探针注册表。新增一个网盘 = 新增一个探针并在此注册。"""

from __future__ import annotations

from collections.abc import Callable

from funflix.base.enums import CHECKABLE_PROVIDERS, Provider
from funflix.services.verify.alipan import AlipanProbe
from funflix.services.verify.base import LinkProbe
from funflix.services.verify.ctfile import CTFileProbe
from funflix.services.verify.pan123 import Pan123Probe
from funflix.services.verify.quark import QuarkProbe
from funflix.services.verify.uc import UCProbe

_REGISTRY: dict[Provider, Callable[[], LinkProbe]] = {
    Provider.QUARK: QuarkProbe,
    Provider.ALIPAN: AlipanProbe,
    Provider.UC: UCProbe,
    Provider.PAN123: Pan123Probe,
    Provider.CTFILE: CTFileProbe,
}


def get_probe(provider: Provider) -> LinkProbe | None:
    """按网盘类型创建对应的探针实例。

    Args:
        provider: 网盘类型。

    Returns:
        该网盘注册的探针实例；未注册时返回 `None`（而不是抛异常），由调用方
        据此把结论判成 UNSUPPORTED。
    """
    factory = _REGISTRY.get(provider)
    return factory() if factory else None


def supported_providers() -> list[Provider]:
    """列出当前已注册探针、可以被校验的网盘类型。

    Returns:
        按枚举值排序后的网盘列表。
    """
    return sorted(_REGISTRY, key=lambda p: p.value)


def assert_registry_matches_enum() -> None:
    """注册表与 `CHECKABLE_PROVIDERS` 必须一致。

    两者不一致会导致静默的错误行为：枚举里标成可校验但没有探针，
    资源会一直卡在 unchecked；反过来则永远不会被调度到。
    """
    registered = set(_REGISTRY)
    if registered != set(CHECKABLE_PROVIDERS):
        raise RuntimeError(
            f"探针注册表 {sorted(p.value for p in registered)} 与 "
            f"CHECKABLE_PROVIDERS {sorted(p.value for p in CHECKABLE_PROVIDERS)} 不一致"
        )
