"""跨模块复用的通用出参。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

#: `Page` 的元素类型。用 TypeVar 而不是 PEP 695 的 `class Page[T]` 语法，
#: 是为了保持 SPEC §3 要求的 Python 3.10 下限（PEP 695 要 3.12）。
T = TypeVar("T")

#: 单页最大条数。四个列表接口共用一个上限，避免各写各的又慢慢分叉。
MAX_PAGE_SIZE = 200

#: 最大页码。
#:
#: 不设上限会出事：`offset = (page - 1) * size` 直接用未经约束的 int 相乘，
#: 传一个 20 位的 page 会让驱动抛 OverflowError（SQLite 是
#: "Python int too large to convert"），穿透路由变成 500 而不是 422。
#: size 本来就有上限，page 却没有 —— 两个乘数只护住了一个。
#:
#: 取 10 万：配合 MAX_PAGE_SIZE 足够翻到两千万行，而深翻页本身
#: 就是 OFFSET 逐行跳过，再大也没有实际用处。
MAX_PAGE_NUMBER = 100_000


class Page(BaseModel, Generic[T]):
    """统一的翻页信封。所有列表接口都返回这个形状。"""

    items: list[T]
    total: int
    page: int
    size: int
