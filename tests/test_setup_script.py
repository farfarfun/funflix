"""`scripts/setup.sh` 的行为测试（来自 farfarfun/todo-list#831 发现 2、3）。

SPEC §6.1 对服务脚本有三条硬约束，正好对应这里的三组用例：

* `start prod` / `run prod` 只能跑已安装的正式包，缺失必须非 0 退出，不回退到源码；
* 后台启动要确认进程真的活着才报告成功，启动即退出必须非 0；
* 重复启动必须以失败状态拒绝。

脚本被复制到 tmp 目录后再执行（它用 `dirname "$0"/..` 定位根目录），
这样 `.run/` 落在 tmp 里，不会污染工作树。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """把 setup.sh 复制进 tmp 目录，返回它的“仓库根”。"""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(SETUP_SH, tmp_path / "scripts" / "setup.sh")
    return tmp_path


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """一个只含基础命令的干净 PATH 目录，测试按需往里塞假的 funflix。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # setup.sh 真正外部调用到的命令，缺一个就会以无关的 "command not found" 失败。
    needed = (
        "bash",
        "sh",
        "env",
        "dirname",
        "cat",
        "tail",
        "tr",
        "grep",
        "rm",
        "rmdir",
        "mkdir",
        "sleep",
        "kill",
        "nohup",
    )
    for name in needed:
        found = shutil.which(name)
        if found:
            (bin_dir / name).symlink_to(found)
    return bin_dir


def _write_fake_funflix(bin_dir: Path, body: str) -> None:
    script = bin_dir / "funflix"
    script.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    script.chmod(0o755)


def _run(sandbox: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = str(bin_dir)
    env["FUNFLIX_START_WAIT"] = "1"
    return subprocess.run(
        [str(sandbox / "scripts" / "setup.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_script_syntax_is_valid() -> None:
    """SPEC §6.4：脚本必须能通过 `bash -n`。"""
    subprocess.run(["bash", "-n", str(SETUP_SH)], check=True)


def test_usage_exits_nonzero(sandbox: Path, fake_bin: Path) -> None:
    """不带参数、或环境名非法时给用法并非 0 退出。"""
    assert _run(sandbox, fake_bin).returncode != 0
    assert _run(sandbox, fake_bin, "start", "staging").returncode != 0


def test_start_prod_without_installed_package_fails(sandbox: Path, fake_bin: Path) -> None:
    """SPEC §6.1：prod 下找不到 funflix 正式包必须立刻报错退出。"""
    result = _run(sandbox, fake_bin, "start", "prod")
    assert result.returncode != 0
    assert "funflix" in result.stderr
    assert not (sandbox / ".run" / "funflix-worker.pid").exists()


def test_run_prod_without_installed_package_fails(sandbox: Path, fake_bin: Path) -> None:
    """前台的 `run prod` 同样不能回退到源码。"""
    result = _run(sandbox, fake_bin, "run", "prod")
    assert result.returncode != 0
    assert "正式包" in result.stderr


def test_start_dev_without_uv_fails(sandbox: Path, fake_bin: Path) -> None:
    """dev 走 `uv run`，uv 不在 PATH 上时要明确报错而不是留下空壳 PID。"""
    result = _run(sandbox, fake_bin, "start", "dev")
    assert result.returncode != 0
    assert "uv" in result.stderr
    assert not (sandbox / ".run" / "funflix-worker.pid").exists()


def test_start_reports_failure_when_worker_exits_immediately(sandbox: Path, fake_bin: Path) -> None:
    """启动即退出必须非 0 并回显日志，不能先打印“已启动”。"""
    _write_fake_funflix(fake_bin, 'echo "boom: 数据库连不上" >&2\nexit 1')
    result = _run(sandbox, fake_bin, "start", "prod")
    assert result.returncode != 0
    assert "立即退出" in result.stderr
    assert "boom" in result.stderr
    assert not (sandbox / ".run" / "funflix-worker.pid").exists()


def test_start_then_duplicate_start_is_rejected(sandbox: Path, fake_bin: Path) -> None:
    """SPEC §6.1：重复启动要以失败状态拒绝，且不能覆盖已有 PID 文件。"""
    _write_fake_funflix(fake_bin, "sleep 30")
    pid_file = sandbox / ".run" / "funflix-worker.pid"
    try:
        first = _run(sandbox, fake_bin, "start", "prod")
        assert first.returncode == 0, first.stderr
        assert pid_file.exists()
        pid = pid_file.read_text(encoding="utf-8").strip()

        duplicate = _run(sandbox, fake_bin, "start", "prod")
        assert duplicate.returncode != 0
        assert "拒绝重复启动" in duplicate.stderr
        assert pid_file.read_text(encoding="utf-8").strip() == pid

        assert _run(sandbox, fake_bin, "status").returncode == 0
    finally:
        _run(sandbox, fake_bin, "stop", "prod")
    assert not pid_file.exists()


def test_status_without_worker_exits_nonzero(sandbox: Path, fake_bin: Path) -> None:
    """没有 worker 在跑时 `status` 返回非 0，便于脚本串联判断。"""
    result = _run(sandbox, fake_bin, "status")
    assert result.returncode != 0
    assert "未运行" in result.stdout


def test_stale_pid_file_is_cleared(sandbox: Path, fake_bin: Path) -> None:
    """PID 文件指向已退出的进程时按陈旧处理，而不是当成"正在运行"。"""
    run_dir = sandbox / ".run"
    run_dir.mkdir()
    # PID 1 是 init，肯定存在但 cmdline 里不会有 "funflix worker"。
    (run_dir / "funflix-worker.pid").write_text("1\n", encoding="utf-8")
    _write_fake_funflix(fake_bin, "sleep 30")
    try:
        result = _run(sandbox, fake_bin, "start", "prod")
        assert result.returncode == 0, result.stderr
        assert "陈旧 PID 文件" in result.stdout
    finally:
        _run(sandbox, fake_bin, "stop", "prod")
