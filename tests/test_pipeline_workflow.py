"""`.github/workflows/collect.yml` 与 CLI 之间的契约。

这套流水线是**无人值守**的：每 2 小时一轮，每个节点刷一小批，没人盯着看。
于是它和普通 CI 有个要命的区别 —— yml 写错不会在本地暴露，要等到定时任务
跑红（甚至更糟：绿着跑，但什么都没干）才看得出来。几类具体的失效：

* 命令或选项拼错 / 被重命名 —— typer 退出码非 0，节点整轮白跑；
* 该传的 `--apply` 漏了 —— 命令变成 dry-run，**绿着什么也不写**，最隐蔽；
* 该传的 `--yes` 漏了 —— 等一个永远不会来的确认输入，直到 timeout；
* `uv run` 漏了 extra —— 见 `TestExtras`，算出和本地不一样的归一键；
* 两个 job 共用一个 concurrency group —— push 时互相取消，只剩一个真的在跑。

所以这里把 yml 当代码测：解析出每一条 `uv` 调用，逐个拿去跟 click 的命令树
核对，再核对几条「怎么调」的约定。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import pytest
import typer
import yaml

from funflix.cli import app

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PIPELINE_YML = WORKFLOW_DIR / "collect.yml"
WATCHDOG_YML = WORKFLOW_DIR / "pipeline-watchdog.yml"

#: `${{ ... }}` 里可以出现空格（`${{ matrix.shard }}`），不先折掉的话
#: shlex 会把一个参数拆成三个 token。折成 `<matrix.shard>` 既不含空格，
#: 又保留了引用的是谁 —— 分片那条用例要靠它认出矩阵变量。
_GH_EXPR = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")

#: uv 自己那些「选项带一个值」的参数。剥 uv 选项时要连值一起跳过，
#: 否则值会被当成 uv 后面要跑的命令。
_UV_VALUE_OPTS = frozenset({"--extra", "--group", "--with", "--python", "--directory"})


def _expand(script: str, env: dict[str, str]) -> str:
    """把 workflow 级 `env:` 和 GitHub 表达式替换掉，让每行能被 shlex 切开。"""
    text = _GH_EXPR.sub(lambda m: f"<{m.group(1)}>", script)
    for key, value in env.items():
        text = text.replace(f"${key}", value).replace(f"${{{key}}}", value)
    return text


@dataclass(frozen=True)
class UvCall:
    """yml 里的一条 `uv run` / `uv sync`。"""

    job: str
    step: str
    verb: str
    extras: frozenset[str]
    #: uv 自己的选项已剥掉，剩下的就是它要跑的那条命令及其参数。
    #: `uv sync` 不跑命令，这里是空的。
    argv: tuple[str, ...]

    @property
    def where(self) -> str:
        return f"{self.job} / {self.step}"


def _parse_uv_line(job: str, step: str, line: str) -> UvCall | None:
    tokens = shlex.split(line)
    if len(tokens) < 2 or tokens[0] != "uv":
        return None
    verb, rest = tokens[1], tokens[2:]
    extras: set[str] = set()
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        option = rest[index]
        if option == "--extra":
            extras.add(rest[index + 1])
            index += 2
        elif option in _UV_VALUE_OPTS:
            index += 2
        else:
            index += 1
    return UvCall(job, step, verb, frozenset(extras), tuple(rest[index:]))


def _load(path: Path) -> dict[str, Any]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    # YAML 1.1 把裸 `on` 当布尔真，于是 `on:` 这个键会变成 `True`。
    # GitHub 按 YAML 1.2 解析（那里只有 `true` 才是布尔），所以 yml 本身没问题，
    # 是 PyYAML 这边需要把键名还原回来。
    if True in spec:
        spec["on"] = spec.pop(True)
    return spec


def _uv_calls(spec: dict[str, Any]) -> list[UvCall]:
    env = {k: str(v) for k, v in (spec.get("env") or {}).items()}
    calls: list[UvCall] = []
    for job_name, job in spec["jobs"].items():
        for step in job.get("steps", []):
            script = step.get("run")
            if not script:
                continue
            step_name = step.get("name", step.get("uses", "<unnamed>"))
            for line in _expand(script, env).splitlines():
                stripped = line.strip()
                # 只切 uv 那几行。`Load secrets` 那步里有 `$(printf ...)`、管道、
                # 字符集方括号，拿 shlex 去切整段脚本是自找麻烦。
                if not stripped.startswith("uv "):
                    continue
                call = _parse_uv_line(job_name, step_name, stripped)
                if call is not None:
                    calls.append(call)
    return calls


PIPELINE = _load(PIPELINE_YML)
JOBS: dict[str, Any] = PIPELINE["jobs"]
UV_CALLS = _uv_calls(PIPELINE)
#: `uv run ... funflix <sub> ...` 的那些。`funsecret load` 不在其列。
FUNFLIX_CALLS = tuple(c for c in UV_CALLS if c.argv[:1] == ("funflix",))

ROOT_COMMAND = typer.main.get_command(app)


def _resolve(argv: tuple[str, ...]) -> tuple[click.Command, tuple[str, ...]]:
    """沿命令树走，返回最终命令和剩下的参数。走不通就直接 fail。"""
    node: click.Command = ROOT_COMMAND
    path = [argv[0]]
    rest = list(argv[1:])
    while rest and isinstance(node, click.Group):
        candidate = node.commands.get(rest[0])
        if candidate is None:
            if rest[0].startswith("-"):
                break
            pytest.fail(f"`{' '.join(path)}` 下没有子命令 `{rest[0]}`")
        node = candidate
        path.append(rest.pop(0))
    return node, tuple(rest)


def _option_names(command: click.Command) -> frozenset[str]:
    return frozenset(opt for param in command.params for opt in param.opts)


def _passed_options(args: tuple[str, ...]) -> frozenset[str]:
    return frozenset(a.split("=", 1)[0] for a in args if a.startswith("-"))


def _ids(calls: tuple[UvCall, ...]) -> list[str]:
    return [" ".join(c.argv) for c in calls]


class TestJobIsolation:
    """每个节点消费自己的队列，所以必须真的能独立跑、互不干扰。"""

    @pytest.mark.parametrize("name", list(JOBS))
    def test_every_job_has_a_timeout(self, name: str) -> None:
        """没有 timeout 的 job 卡住会占满 6 小时的默认上限。

        这几个节点都在网络上做大量往返，卡死不是假想情况。
        """
        assert JOBS[name].get("timeout-minutes"), f"{name} 没设 timeout-minutes"

    @pytest.mark.parametrize("name", list(JOBS))
    def test_every_job_has_its_own_concurrency_group(self, name: str) -> None:
        """组名必须按 job（和矩阵维度）区分开。

        GitHub 的 concurrency group 是按**字符串**归组的，跨 job 也归。两个 job
        写同一个组名，push 触发时后启动的会把前一个取消掉 —— 日志里看着像
        「某个节点偶尔莫名其妙被 cancelled」，很难往这儿想。
        """
        group = JOBS[name].get("concurrency", {}).get("group")
        assert group, f"{name} 没设 concurrency.group"

        # 矩阵 job 的组名必须带上每一个矩阵维度，否则各片归到同一组、互相取消。
        for axis in JOBS[name].get("strategy", {}).get("matrix") or {}:
            assert f"<matrix.{axis}>" in _GH_EXPR.sub(lambda m: f"<{m.group(1)}>", group), (
                f"{name} 按 {axis} 分了矩阵，concurrency.group 却没带上它"
            )

    def test_concurrency_groups_are_distinct(self) -> None:
        groups = [job["concurrency"]["group"] for job in JOBS.values()]
        assert len(set(groups)) == len(groups), f"有 job 共用 concurrency.group：{groups}"

    @pytest.mark.parametrize("name", list(JOBS))
    def test_no_job_waits_on_another(self, name: str) -> None:
        """刻意不编排。

        各节点消费的是不同的队列（采集水位 / 待解析文档 / 待校验资源 /
        待裁决候选块 / 待修复 media），谁都不需要等别人这一轮的产出才能开工。
        串成 `needs` 链只会让一步卡住拖着后面几步一起等。
        """
        assert "needs" not in JOBS[name]


class TestExtras:
    """`zh` extra 漏一处就会算出不一样的归一键。

    `normalize._to_simplified()` 在 opencc 缺失时**原样返回、不报错**，而
    `norm_key` / `series_norm_key` 都要过它。装了 opencc 的环境把
    `唐伯虎點秋香` 归一到「点」，没装的归一到「點」，同一部剧于是在两处
    解析出两行 work —— 正是这套流水线要消灭的那种重复。

    偏偏 `uv run` 每次都会按**本次请求的** extra 重新同步环境：前一步
    `uv sync --extra zh` 装好了 opencc，下一步一条光秃秃的 `uv run` 会把它
    卸掉。所以同一个 job 里每一条 uv 调用的 extra 必须完全一致。
    """

    def test_every_job_requests_the_zh_extra(self) -> None:
        jobs_with_uv = {c.job for c in UV_CALLS}
        assert jobs_with_uv == set(JOBS), "有 job 完全没装依赖"
        for job in jobs_with_uv:
            for call in (c for c in UV_CALLS if c.job == job):
                assert "zh" in call.extras, f"{call.where}：`uv {call.verb}` 少了 --extra zh"

    @pytest.mark.parametrize("job", list(JOBS))
    def test_extras_are_identical_within_a_job(self, job: str) -> None:
        by_extras: dict[frozenset[str], list[str]] = {}
        for call in (c for c in UV_CALLS if c.job == job):
            by_extras.setdefault(call.extras, []).append(call.where)
        assert len(by_extras) == 1, (
            f"{job} 里各条 uv 调用请求的 extra 不一致，后一条会把前一条装的卸掉："
            f"{ {sorted(k): v for k, v in by_extras.items()} }"
        )

    def test_canon_is_the_only_job_needing_llm(self) -> None:
        """`llm` extra（openai 客户端）只有 `canon resolve` 用得上。

        其余节点装它纯属浪费安装时间，而漏装会让 `canon resolve` 在 import
        时就炸。
        """
        with_llm = {c.job for c in UV_CALLS if "llm" in c.extras}
        assert with_llm == {"canon"}


class TestCliContract:
    """yml 里写的每条命令、每个选项，CLI 里都得真有。"""

    @pytest.mark.parametrize("call", FUNFLIX_CALLS, ids=_ids(FUNFLIX_CALLS))
    def test_command_exists(self, call: UvCall) -> None:
        command, _ = _resolve(call.argv)
        assert not isinstance(command, click.Group), (
            f"{call.where}：`{' '.join(call.argv)}` 停在命令组上，没指到具体子命令"
        )

    @pytest.mark.parametrize("call", FUNFLIX_CALLS, ids=_ids(FUNFLIX_CALLS))
    def test_options_exist(self, call: UvCall) -> None:
        command, args = _resolve(call.argv)
        known = _option_names(command)
        unknown = _passed_options(args) - known
        assert not unknown, (
            f"{call.where}：`{' '.join(call.argv)}` 用了不存在的选项 {sorted(unknown)}，"
            f"可选的是 {sorted(known)}"
        )


class TestInvocationConventions:
    """「怎么调」的约定 —— 漏了不一定报错，但节点会白跑或者挂住。"""

    @pytest.mark.parametrize("call", FUNFLIX_CALLS, ids=_ids(FUNFLIX_CALLS))
    def test_write_commands_pass_apply(self, call: UvCall) -> None:
        """有 `--apply` 的命令必须传 `--apply`。

        不传就是 dry-run：**退出码 0、日志好看、一个字节都没写**。这种失效
        在无人值守的流水线里能瞒好几天 —— 队列不见少才是唯一的线索。
        """
        command, args = _resolve(call.argv)
        if "--apply" not in _option_names(command):
            return
        assert "--apply" in _passed_options(args), (
            f"{call.where}：`{' '.join(call.argv)}` 漏了 --apply，这一步只会空跑"
        )

    @pytest.mark.parametrize("call", FUNFLIX_CALLS, ids=_ids(FUNFLIX_CALLS))
    def test_interactive_commands_are_pre_confirmed(self, call: UvCall) -> None:
        """有 `--yes` 的命令必须传 `--yes`。

        runner 上没有 tty，漏传的话命令会停在那个永远不会被回答的确认提示上，
        直到 120 分钟 timeout 把 job 掐掉。
        """
        command, args = _resolve(call.argv)
        if "--yes" not in _option_names(command):
            return
        assert "--yes" in _passed_options(args), (
            f"{call.where}：`{' '.join(call.argv)}` 漏了 --yes，在无 tty 的 runner 上会挂住"
        )

    #: `repair scan` 刻意不限量：它的翻页游标不跨进程持久化、每轮都从
    #: `media.id` 最小的那头重新开始，加了 `--limit` 就等于永远只扫前 N 行、
    #: 后面的数据再也修不到。全表按主键翻页本身有界，两小时一轮扛得住。
    _UNTHROTTLED = {
        ("funflix", "repair", "scan"),
        # `db relink-checks` 同样有界：一条流式查询读出每个链接的最新结论、
        # 按批 executemany 回填，没有 `--limit` 选项可传。
        ("funflix", "db", "relink-checks"),
    }

    #: 单轮闸门的几种形式，有一个就算合格。
    #:
    #: `--max-seconds` 和 `--limit` 一样是闸门，只是量的是时间而不是条数 ——
    #: `verify` 用的就是它：那条流水线的吞吐由最慢那个网盘的限速决定，
    #: 「多少条」能跑多久取决于待校验队列里各网盘的占比，用条数算时间算不准，
    #: 算多了就是整轮撞 `timeout-minutes` 被判 cancelled。
    _THROTTLES = ("--limit", "--max-seconds")

    @pytest.mark.parametrize("call", FUNFLIX_CALLS, ids=_ids(FUNFLIX_CALLS))
    def test_queue_consumers_are_throttled(self, call: UvCall) -> None:
        """能限量/限时的命令都得带上闸门 —— 除了上面那两个有记录在案的例外。

        闸门是单轮处理量的上限：踩到脏数据死循环重试时，影响范围限于这一批
        而不是整个队列；`canon resolve` 更直接，它是**钱**的闸门。
        """
        command, args = _resolve(call.argv)
        available = [opt for opt in self._THROTTLES if opt in _option_names(command)]
        if not available:
            return
        passed = _passed_options(args)
        if any(call.argv[: len(prefix)] == prefix for prefix in self._UNTHROTTLED):
            assert not [opt for opt in available if opt in passed], (
                f"{call.where}：`{' '.join(call.argv)}` 不该限量，见 _UNTHROTTLED 的说明"
            )
            return
        assert [opt for opt in available if opt in passed], (
            f"{call.where}：`{' '.join(call.argv)}` 漏了闸门（{' / '.join(available)} 任选其一）"
        )

    def test_repair_apply_does_not_force(self) -> None:
        """爆炸半径闸门在自动流水线上不能被绕过。

        `repair apply` 的 `--force` 会关掉「删除 > 全库 5% 或重挂 > 20% 就
        拒绝」这道检查。那是无人值守时的最后一道防线：一次规则写错不该被自动
        放行，该让这一步红掉、等人去核对 scan 吐出来的样例。
        """
        applies = [c for c in FUNFLIX_CALLS if c.argv[:3] == ("funflix", "repair", "apply")]
        assert applies, "流水线里没有 `repair apply`"
        for call in applies:
            assert "--force" not in call.argv, f"{call.where}：自动流水线里不该传 --force"


class TestParseSharding:
    """分片必须是**恰好覆盖一次**：漏一片只是留在 pending，重一片会解析两遍。"""

    def test_shard_axis_is_a_zero_based_range(self) -> None:
        shards = JOBS["parse"]["strategy"]["matrix"]["shard"]
        assert shards == list(range(len(shards))), (
            f"分片号必须是 0..N-1 的完整序列，现在是 {shards}；"
            "`shard_condition` 按 `id` 末位十六进制字符取模，缺号的那片文档没人解析"
        )

    def test_shard_count_matches_the_matrix(self) -> None:
        total = len(JOBS["parse"]["strategy"]["matrix"]["shard"])
        parses = [c for c in FUNFLIX_CALLS if c.argv[:2] == ("funflix", "parse")]
        assert parses, "流水线里没有 `parse`"
        for call in parses:
            args = call.argv
            value = args[args.index("--shard") + 1]
            assert value == f"<matrix.shard>/{total}", (
                f"{call.where}：`--shard {value}` 的分母和矩阵的 {total} 片对不上 —— "
                "分母偏小会有文档被多片重复解析，偏大会有整片文档没人解析"
            )

    def test_shards_do_not_fail_fast(self) -> None:
        """一片挂了不该掐掉其它片 —— 每片是独立的一批文档，没有共享产出。"""
        assert JOBS["parse"]["strategy"]["fail-fast"] is False


class TestWatchdogAgreesWithSchedule:
    """`pipeline-watchdog.yml` 里硬编码的阈值得跟 collect.yml 的 cron 对得上。

    看门狗的作用是补上被 GitHub 跳过的那一轮（平台负载高时 schedule 会被延迟
    甚至整轮跳过，且不留 run 记录）。阈值一旦低于正常触发间隔，它就会在**没出
    任何问题**的时候反复补触发；改 cron 不改阈值正是这么犯的。
    """

    def test_threshold_exceeds_the_schedule_interval(self) -> None:
        crons = [entry["cron"] for entry in PIPELINE["on"]["schedule"]]
        assert len(crons) == 1, f"看门狗的阈值只按单条 cron 算，现在有 {crons}"
        hour_field = crons[0].split()[1]
        assert hour_field.startswith("*/"), f"看门狗只会算 `*/N` 式的小时间隔：{hour_field}"
        interval_min = int(hour_field[2:]) * 60

        script = WATCHDOG_YML.read_text(encoding="utf-8")
        match = re.search(r"^\s*threshold_min=(\d+)", script, re.MULTILINE)
        assert match, "pipeline-watchdog.yml 里找不到 threshold_min"
        threshold = int(match.group(1))

        assert threshold > interval_min, (
            f"阈值 {threshold} 分钟不大于正常间隔 {interval_min} 分钟，"
            "看门狗会在流水线正常时反复补触发"
        )

    def test_watchdog_targets_an_existing_workflow(self) -> None:
        """看门狗是按**文件名**找 collect.yml 的，改名不会有任何报错。"""
        script = WATCHDOG_YML.read_text(encoding="utf-8")
        referenced = set(re.findall(r"[\w.-]+\.yml", script))
        assert referenced, "看门狗没引用任何 workflow 文件"
        for name in referenced:
            assert (WORKFLOW_DIR / name).exists(), f"看门狗引用了不存在的 {name}"
