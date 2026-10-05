"""`funflix.compat` 垫片与 Python 版本下限的回归测试（来自 farfarfun/todo-list#831）。

包的 `requires-python` 是 `>=3.10`（SPEC §3），但源码里曾经直接用了
PEP 695 类型参数语法（3.12）、`enum.StrEnum` 和 `datetime.UTC`（3.11），
在 3.10 上是语法错误 / ImportError。这里从三个角度锁住：

1. 垫片导出的 `StrEnum` 行为与标准库一致；
2. 全仓库不再出现 PEP 695 语法和对 3.11+ 名字的直接导入；
3. `pyproject.toml` 的 `requires-python`、classifiers、Ruff `target-version` 互相自洽。
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from enum import auto
from pathlib import Path

import pytest

from funflix.compat import UTC, StrEnum

REPO_ROOT = Path(__file__).resolve().parent.parent
PY_FILES = sorted(p for p in (REPO_ROOT / "src").rglob("*.py") if "__pycache__" not in p.parts)


def test_utc_is_utc() -> None:
    """垫片里的 UTC 必须就是零偏移时区。"""
    assert UTC.utcoffset(None) == timedelta(0)
    assert datetime(2026, 1, 1, tzinfo=UTC).isoformat().endswith("+00:00")
    assert datetime(2026, 1, 1, tzinfo=UTC) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_str_enum_members_are_str() -> None:
    """成员同时是 str，可以直接参与字符串比较与字典取值。"""

    class Color(StrEnum):
        RED = "red"

    assert isinstance(Color.RED, str)
    assert Color.RED == "red"
    assert {"red": 1}[Color.RED] == 1


def test_str_enum_str_and_format_return_value() -> None:
    """`str()` / f-string 取的是成员值，不是 `Color.RED`。

    这是 `StrEnum` 与朴素的 `class X(str, Enum)` 最关键的差别：
    后者 `str(X.RED)` 会得到 `"Color.RED"`，日志和入库值会全错。
    """

    class Color(StrEnum):
        RED = "red"

    assert str(Color.RED) == "red"
    assert f"{Color.RED}" == "red"
    assert "{}".format(Color.RED) == "red"  # noqa: UP032 - 显式测试 format 协议


def test_str_enum_auto_lowercases_name() -> None:
    """`auto()` 生成小写成员名，与标准库 `StrEnum` 一致。"""

    class Color(StrEnum):
        DARK_RED = auto()

    assert Color.DARK_RED.value == "dark_red"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="3.11+ 才有标准库 StrEnum")
def test_shim_delegates_to_stdlib_on_new_pythons() -> None:
    """3.11 及以上直接复用标准库实现，不走自己的 fallback。"""
    import enum

    assert StrEnum is enum.StrEnum


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_pep695_type_params(path: Path) -> None:
    """PEP 695 的 `def f[T]()` / `class C[T]` 要 3.12，3.10 上直接语法错误。"""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert not getattr(node, "type_params", ()), (
                f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.name} 用了 PEP 695 "
                f"类型参数语法，请改成 typing.TypeVar"
            )


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_direct_311_only_imports(path: Path) -> None:
    """`enum.StrEnum` / `datetime.UTC` 必须从 `funflix.compat` 取。"""
    if path.name == "compat.py":
        return
    banned = {"enum": {"StrEnum"}, "datetime": {"UTC"}}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom):
            continue
        for name in banned.get(node.module or "", set()):
            assert name not in {a.name for a in node.names}, (
                f"{path.relative_to(REPO_ROOT)}:{node.lineno} 直接从 {node.module} 导入 "
                f"{name}（3.11+ 才有），请改为 `from funflix.compat import {name}`"
            )


def test_pyproject_python_floor_is_consistent() -> None:
    """requires-python / classifiers / Ruff target-version 三处必须对得上。

    不用 tomllib 解析：它要 3.11，而这个测试恰恰需要能在 3.10 上跑起来。
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in text
    assert '"Programming Language :: Python :: 3.10"' in text
    assert 'target-version = "py310"' in text
