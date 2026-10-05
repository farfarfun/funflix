"""`pyproject.toml` 里几处版本声明的自洽性回归（来自 farfarfun/todo-list#831）。

`requires-python`、classifiers 与 Ruff 的 `target-version` 说的是同一件事
（本包支持哪些 Python），但分散在三处，改一处漏两处不会有任何报错 ——
Ruff 的 `target-version` 落后尤其阴险：它会按旧版本的语法上限来检查，
新语法能用却被标成错误，或者反过来放过在声明下限上跑不起来的写法。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

#: 全组织统一的 Python 下限。funflix-api 也是这个值。
PYTHON_FLOOR = (3, 12)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_python_floor_is_consistent() -> None:
    """requires-python / classifiers / Ruff target-version 三处必须对得上。"""
    major, minor = PYTHON_FLOOR
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert cfg["project"]["requires-python"] == f">={major}.{minor}"
    assert cfg["tool"]["ruff"]["target-version"] == f"py{major}{minor}"

    # classifiers 不要求穷举到最新的解释器，但声明的最低那个必须就是下限 ——
    # 低于下限的 classifier 会让 pip 在装不上的环境里才报错。
    declared = sorted(
        tuple(int(x) for x in c.rsplit(" :: ", 1)[-1].split("."))
        for c in cfg["project"]["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    )
    assert declared, "classifiers 里一个 Python 版本都没声明"
    assert declared[0] == PYTHON_FLOOR
