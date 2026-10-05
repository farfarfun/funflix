"""`scripts/canon-rollout.sh` 的行为测试。

这个脚本改的是生产库的 89 万行 media，而误并不可逆 —— 两部剧并成一部之后没有
信息能把它们分回去。所以这里测的不是"功能对不对"，而是四条**安全属性**：

* `check` 一个字都不写：传给 funflix 的参数里不能出现 `--apply`；
* 非交互环境下人工闸门必须拒绝继续（不能因为"没人看着"就默认放行），
  且拒绝之后不能留下阶段断点 —— 否则重跑会把没人核对过的阶段当成已完成；
* 阶段断点能跳过已完成的阶段，这是几小时的流程能中断续跑的前提；
* 阶段名/参数写错要立刻非 0 退出，而不是跑到一半才发现。

psql / python / funflix 全部用假的：测试不碰数据库、不发 LLM 调用、不碰 Action。
脚本被复制到 tmp 目录后执行（它用 `dirname "$0"/..` 定位根目录），这样 `.run/`
落在 tmp 里，不会污染工作树。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLLOUT_SH = REPO_ROOT / "scripts" / "canon-rollout.sh"

STAGES = (
    "backup", "guard", "rehearse", "purge", "rebuild", "resolve", "merge", "finalize", "reopen",
)

#: 假密码取个不可能自然出现的串，这样"日志里没有它"是个有意义的断言。
FAKE_PASSWORD = "s3cr3t-must-not-be-logged"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """把脚本复制进 tmp 目录，返回它的“仓库根”。"""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(ROLLOUT_SH, tmp_path / "scripts" / "canon-rollout.sh")
    return tmp_path


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """只含基础命令的干净 PATH 目录；psql 故意不在里面，由各用例按需塞。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    needed = (
        "bash",
        "sh",
        "env",
        "cat",
        "date",
        "dirname",
        "mkdir",
        "rm",
        "tee",
        "tr",
        "awk",
        "df",
    )
    for name in needed:
        found = shutil.which(name)
        if found:
            (bin_dir / name).symlink_to(found)
    return bin_dir


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_pg(tmp_path: Path, fake_bin: Path) -> Path:
    """假的 psql + 假的 python，让 pg_connect 能走通而不连任何数据库。

    脚本按 `python - <pgpass 路径> <pgenv 路径>` 调用，参数而非 stdout 传路径
    （真实实现这么做是因为 funsecret 会往 stdout 打日志），所以这里按 $2/$3 落文件。
    """
    # 一律答 0：stage_finalize 的两道前置检查（还有多少行没归属、多少组
    # (work_id, season) 撞车）读的是 `psql -tAc` 的输出，答空串会被当成"查出来不是 0"。
    _write_exec(fake_bin / "psql", 'cat >/dev/null 2>&1 || true\necho 0')
    return _write_exec(
        tmp_path / "fake-python",
        'cat >/dev/null\n'
        f'printf "h:5432:db:u:{FAKE_PASSWORD}\\n" >"$2"\n'
        'printf "PGHOST=h\\nPGPORT=5432\\nPGDATABASE=db\\nPGUSER=u\\n" >"$3"',
    )


@pytest.fixture
def calls(tmp_path: Path) -> Path:
    """假 funflix 把每次调用的参数追加到这个文件，用来断言传了什么。"""
    return tmp_path / "funflix-calls.txt"


@pytest.fixture
def fake_funflix(tmp_path: Path, calls: Path) -> Path:
    return _write_exec(tmp_path / "fake-funflix", f'echo "$*" >>{calls}')


def _run(
    sandbox: Path,
    fake_bin: Path,
    *args: str,
    funflix: Path | None = None,
    python: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = str(fake_bin)
    env["HOME"] = str(sandbox / "home")
    if funflix:
        env["FUNFLIX_CMD"] = str(funflix)
    if python:
        env["FUNFLIX_PY"] = str(python)
    return subprocess.run(
        [str(sandbox / "scripts" / "canon-rollout.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        # stdin 是管道而不是终端 —— 正好是人工闸门必须拒绝放行的那种环境。
        stdin=subprocess.DEVNULL,
        timeout=120,
    )


def _markers(sandbox: Path) -> set[str]:
    run_dir = sandbox / ".run" / "canon-rollout"
    return {p.stem for p in run_dir.glob("*.done")} if run_dir.is_dir() else set()


def test_script_syntax_is_valid() -> None:
    """SPEC §6.4：脚本必须能通过 `bash -n`。"""
    subprocess.run(["bash", "-n", str(ROLLOUT_SH)], check=True)


def test_usage_exits_two(sandbox: Path, fake_bin: Path) -> None:
    """不带参数、或动作名不认识时给用法并以 2 退出。"""
    bare = _run(sandbox, fake_bin)
    assert bare.returncode == 2
    assert "canon-rollout.sh apply" in bare.stdout

    assert _run(sandbox, fake_bin, "frobnicate").returncode == 2


@pytest.mark.parametrize(
    ("args", "expect"),
    [
        (("apply", "--from"), "--from 要跟一个阶段名"),
        (("apply", "--from", "nosuchstage"), "没有这个阶段"),
        (("apply", "--force-push"), "未知参数"),
    ],
)
def test_bad_arguments_fail_before_touching_anything(
    sandbox: Path, fake_bin: Path, args: tuple[str, ...], expect: str
) -> None:
    """参数错误要在连库之前就报掉 —— 跑到一半才发现阶段名拼错是最糟的。"""
    result = _run(sandbox, fake_bin, *args)
    assert result.returncode != 0
    assert expect in result.stderr
    assert _markers(sandbox) == set()


def test_no_psql_gives_actionable_error(sandbox: Path, fake_bin: Path) -> None:
    """psql 不在 PATH 上时要指名道姓说装什么，而不是 command not found。"""
    result = _run(sandbox, fake_bin, "status")
    assert result.returncode != 0
    assert "postgresql-client" in result.stdout + result.stderr


def test_check_never_passes_apply(
    sandbox: Path, fake_bin: Path, fake_pg: Path, fake_funflix: Path, calls: Path
) -> None:
    """`check` 的全部承诺就是"一个字都没写"：四个阶段都得是默认的 dry-run。"""
    result = _run(sandbox, fake_bin, "check", funflix=fake_funflix, python=fake_pg)
    assert result.returncode == 0, result.stdout

    # 顺序也是承诺的一部分：purge 在 rebuild 之前（垃圾行不清掉会凭空造出
    # 十几万个垃圾 Work），rebuild 在 resolve 之前（规则能搬掉大部分，LLM 只打残局）。
    invocations = calls.read_text(encoding="utf-8").split("\n")[:-1]
    assert invocations == ["canon purge", "canon rebuild", "canon resolve", "canon merge"]
    assert _markers(sandbox) == set(), "check 不该写阶段断点"


def test_gate_refuses_when_stdin_is_not_a_tty(
    sandbox: Path, fake_bin: Path, fake_pg: Path, fake_funflix: Path, calls: Path
) -> None:
    """非交互环境下人工闸门必须拦住，且不能留下断点。

    不加 `--yes` 就在无人值守时继续，等于把"误并不可逆"的风险交给环境凑巧。
    断点更关键：如果中止时写了 `rehearse.done`，重跑会直接跳到全库阶段 ——
    那道人工核对就永远不会发生了。
    """
    result = _run(
        sandbox, fake_bin, "apply", "--from", "rehearse", funflix=fake_funflix, python=fake_pg
    )
    assert result.returncode != 0
    assert "需要人工确认" in result.stdout + result.stderr
    assert "rehearse" not in _markers(sandbox)

    # 闸门之前的单组演练确实只动了一个归一键，没有全库跑。
    invocations = calls.read_text(encoding="utf-8").split("\n")[:-1]
    assert invocations == [
        "canon purge --key 大主宰 --apply --yes",
        "canon rebuild --key 大主宰 --apply --yes",
    ]


def test_yes_passes_the_gate(
    sandbox: Path, fake_bin: Path, fake_pg: Path, fake_funflix: Path, calls: Path
) -> None:
    """`--yes` 是放行的唯一方式，而且放行之后才轮到全库阶段。"""
    result = _run(
        sandbox, fake_bin, "apply", "--from", "rehearse", "--yes", "--skip-guard",
        funflix=fake_funflix, python=fake_pg,
    )
    assert result.returncode == 0, result.stdout
    assert _markers(sandbox) == {
        "rehearse", "purge", "rebuild", "resolve", "merge", "finalize", "reopen",
    }

    invocations = calls.read_text(encoding="utf-8").split("\n")[:-1]
    assert "canon purge --apply --yes" in invocations
    # resolve 的小额探针必须在全量之前：这是真实模型路径第一次在生产数据上跑。
    assert invocations.index("canon resolve --limit 3 --apply --yes") < invocations.index(
        "canon resolve --apply --yes"
    )
    # 迁移 B 收口排在 merge 之后 —— 它要把 work_id 设成 NOT NULL。
    assert invocations.index("canon merge --apply --yes") < invocations.index("db upgrade")


def test_completed_stages_are_skipped(
    sandbox: Path, fake_bin: Path, fake_pg: Path, fake_funflix: Path, calls: Path
) -> None:
    """断点让几小时的流程能中断续跑：已完成的阶段不重跑，也不重新花 token。"""
    first = _run(
        sandbox, fake_bin, "apply", "--from", "reopen", "--skip-guard",
        funflix=fake_funflix, python=fake_pg,
    )
    assert first.returncode == 0, first.stdout
    assert _markers(sandbox) == {"reopen"}

    second = _run(
        sandbox, fake_bin, "apply", "--from", "reopen", "--skip-guard",
        funflix=fake_funflix, python=fake_pg,
    )
    assert second.returncode == 0, second.stdout
    assert "跳过 reopen（已完成于" in second.stdout
    assert not calls.exists(), "reopen 不该调用 funflix"


def test_reset_clears_markers_but_keeps_backup_record(sandbox: Path, fake_bin: Path) -> None:
    """`reset` 清的是进度，不是资产 —— 备份记录和 Action 原状态要留着。"""
    run_dir = sandbox / ".run" / "canon-rollout"
    run_dir.mkdir(parents=True)
    for stage in STAGES:
        (run_dir / f"{stage}.done").write_text("2026-10-05T00:00:00+08:00\n", encoding="utf-8")
    (run_dir / "last-backup").write_text("/somewhere/pre-canon-20261005\n", encoding="utf-8")
    (run_dir / "collect.yml.prev-state").write_text("active\n", encoding="utf-8")

    result = _run(sandbox, fake_bin, "reset")
    assert result.returncode == 0, result.stderr
    assert _markers(sandbox) == set()
    assert (run_dir / "last-backup").exists()
    assert (run_dir / "collect.yml.prev-state").exists()


def test_password_file_is_removed_and_never_printed(
    sandbox: Path, fake_bin: Path, fake_pg: Path, fake_funflix: Path
) -> None:
    """密码只经由 600 权限的 PGPASSFILE 传给 psql，跑完即删、全程不落日志。"""
    result = _run(sandbox, fake_bin, "apply", "--from", "reopen", "--skip-guard",
                  funflix=fake_funflix, python=fake_pg)
    assert result.returncode == 0, result.stdout
    assert not (sandbox / ".run" / "canon-rollout" / ".pgpass").exists()

    log_file = next((sandbox / ".run" / "canon-rollout").glob("rollout-*.log"))
    log = log_file.read_text(encoding="utf-8")
    assert FAKE_PASSWORD not in log
    assert FAKE_PASSWORD not in result.stdout + result.stderr
