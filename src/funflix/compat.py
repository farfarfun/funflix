"""Python 版本兼容垫片。

SPEC §3 要求本组织的包把 ``requires-python`` 压到 ``>=3.10``，
但本仓库用到两个 3.11 才进标准库的名字：

* :class:`enum.StrEnum`（3.11 新增）；
* :data:`datetime.UTC`（3.11 新增，等价于 ``timezone.utc``）。

这里按解释器版本择一导出，业务代码统一从本模块取，
不要再直接 ``from enum import StrEnum`` / ``from datetime import UTC``。
"""

from __future__ import annotations

import sys
from datetime import timezone

__all__ = ["UTC", "StrEnum"]

if sys.version_info >= (3, 11):  # pragma: no cover - 取决于运行时解释器版本
    from datetime import UTC as UTC
    from enum import StrEnum as StrEnum
else:  # pragma: no cover - 取决于运行时解释器版本
    from enum import Enum

    #: ``datetime.UTC`` 在 3.11 才有，3.10 上退回等价的 ``timezone.utc``。
    UTC = timezone.utc

    class StrEnum(str, Enum):
        """``enum.StrEnum`` 在 Python 3.10 上的等价实现。

        与标准库保持一致的三点语义：成员同时是 ``str``；``str(member)``
        返回成员值而不是 ``ClassName.MEMBER``；``auto()`` 生成小写成员名。
        """

        __str__ = str.__str__
        __format__ = str.__format__

        @staticmethod
        def _generate_next_value_(name: str, start: int, count: int, last_values: list[str]) -> str:
            """``auto()`` 取小写成员名，与标准库 ``StrEnum`` 行为一致。"""
            return name.lower()
