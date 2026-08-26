"""校验环节：判断网盘链接现在还能不能用。

原则：能匿名探测就绝不登录。当前实现夸克、阿里云盘两个匿名探针，
其余网盘入库但不校验（check_status=unsupported）。
"""

from funflix.services.verify.alipan import AlipanProbe
from funflix.services.verify.base import CheckOutcome, LinkProbe, LinkRef
from funflix.services.verify.quark import QuarkProbe
from funflix.services.verify.registry import (
    assert_registry_matches_enum,
    get_probe,
    supported_providers,
)
from funflix.services.verify.runner import RateLimiter, VerifyReport, check_resource

__all__ = [
    "AlipanProbe",
    "CheckOutcome",
    "LinkProbe",
    "LinkRef",
    "QuarkProbe",
    "RateLimiter",
    "VerifyReport",
    "assert_registry_matches_enum",
    "check_resource",
    "get_probe",
    "supported_providers",
]
