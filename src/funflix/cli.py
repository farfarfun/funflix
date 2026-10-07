"""funflix 命令行入口。

覆盖整条流水线：采集 → 抽取 → 查询，以及数据库与状态检查。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn

import click
import questionary
import typer
from farlog import getLogger
from sqlalchemy import select
from tqdm import tqdm

from funflix import __version__
from funflix.base.config import get_settings
from funflix.base.enums import CheckStatus, MediaType, ParseStatus, SourceType

if TYPE_CHECKING:
    from funflix.services.sync import SyncReport

logger = getLogger("funflix")

app = typer.Typer(help="funflix 命令行工具")
db_app = typer.Typer(help="数据库迁移与检查", no_args_is_help=True)
source_app = typer.Typer(help="采集源管理与采集", no_args_is_help=True)
sync_app = typer.Typer(help="本地库与远端库同步（自建 self-hosted runner）", no_args_is_help=True)
user_app = typer.Typer(help="登录账号管理", no_args_is_help=True)
canon_app = typer.Typer(help="搜索结果归一（Work 实体 + 季子层）", no_args_is_help=True)
repair_app = typer.Typer(help="持续修复（规则更新后把旧数据刷对）", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(canon_app, name="canon")
app.add_typer(repair_app, name="repair")
app.add_typer(source_app, name="source")
app.add_typer(sync_app, name="sync")
app.add_typer(user_app, name="user")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="打印版本号后退出"
        ),
    ] = False,
) -> None:
    """不带子命令直接执行 `funflix` 时，进交互菜单，而不是打印帮助。"""
    if ctx.invoked_subcommand is None:
        _interactive_menu()


# --- 输出 helpers ------------------------------------------------------------


def _run[T](factory: Callable[[], Awaitable[T]]) -> T:
    """跑一个协程。每条命令都是一次性进程，不需要复用事件循环。"""
    return asyncio.run(factory())


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _ok(message: str) -> None:
    typer.secho(message, fg=typer.colors.GREEN)


def _warn(message: str) -> None:
    typer.secho(message, fg=typer.colors.YELLOW)


def _dim(message: str) -> None:
    typer.secho(message, fg=typer.colors.BRIGHT_BLACK)


def _heading(message: str) -> None:
    typer.secho(message, fg=typer.colors.CYAN, bold=True)


def _width(text: str) -> int:
    """显示宽度。中文占两格，不算的话表格会错位。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _table(rows: list[list[Any]], headers: list[str]) -> None:
    """按列宽对齐打印一张表，表头用暗色。列宽按中文占两格计算。"""
    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max([_width(headers[i])] + [_width(row[i]) for row in cells]) for i in range(len(headers))
    ]

    def render(values: list[str]) -> str:
        """把一行单元格按算好的列宽右侧补空格拼成一行。"""
        return "  ".join(v + " " * (widths[i] - _width(v)) for i, v in enumerate(values))

    _dim(render(headers))
    for row in cells:
        typer.echo(render(row))


def _progress(items: list[Any], desc: str, unit: str) -> Any:
    """统一的进度条。只有多于一项时才显示，避免单条任务被进度条刷屏。"""
    return tqdm(items, desc=desc, unit=unit, leave=False, disable=len(items) <= 1)


# --- status ------------------------------------------------------------------


@app.command()
def status(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="展开采集源明细")] = False,
) -> None:
    """查看流水线各环节的记录数。"""
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import Source
    from funflix.services.stats import PipelineStats, collect_stats

    async def _fetch() -> tuple[PipelineStats, list[Source]]:
        async with session_scope() as session:
            stats = await collect_stats(session)
            # 明细只有 --verbose 才用得到，不值得塞进 collect_stats 的返回里
            sources = list(await session.scalars(select(Source).order_by(Source.id)))
            return stats, sources

    data, sources = _run(_fetch)
    _dim(f"数据库 {get_settings().database_url.split('://', 1)[0]}\n")

    def rows_of(counts: dict[str, int], highlight: set[str] | None = None) -> list[list[Any]]:
        """分档明细转成表格行，占比让「哪一档最堵」一眼可见。"""
        total = sum(counts.values()) or 1
        out = []
        for label, value in sorted(counts.items(), key=lambda kv: -kv[1]):
            mark = " ⚠" if highlight and label in highlight and value else ""
            out.append([label + mark, value, f"{value * 100 / total:.0f}%"])
        return out

    _heading("流水线总览")
    _table(
        [
            [
                "1 采集源 source",
                data.sources_total,
                f"启用 {data.sources_enabled} / 连续失败 {data.sources_failing}",
            ],
            ["2 原始文本 raw_document", data.raw_total, ""],
            ["3 抽取留档 extraction", data.extraction_total, ""],
            ["4 作品 media", data.media_total, ""],
            [
                "5 资源 resource",
                data.resource_total,
                f"未归属 {data.resource_orphan} / 作品↔资源 {data.media_resource_total}",
            ],
            ["6 校验历史 link_check", data.check_total, ""],
        ],
        ["环节", "总数", "备注"],
    )

    sections = [
        (
            "原始文本 · 解析状态",
            data.raw_by_status,
            {ParseStatus.FAILED.value, ParseStatus.PENDING.value},
        ),
        ("抽取留档 · 按抽取器", data.extraction_by_model, None),
        ("作品 · 按类型", data.media_by_type, None),
        (
            "资源 · 按校验状态",
            data.resource_by_check,
            {CheckStatus.INVALID.value, CheckStatus.ERROR.value},
        ),
        ("资源 · 按网盘", data.resource_by_provider, None),
    ]
    for title, counts, highlight in sections:
        typer.echo()
        _heading(title)
        if counts:
            _table(rows_of(counts, highlight), ["项", "数量", "占比"])
        else:
            _dim("  （无记录）")

    if verbose and sources:
        typer.echo()
        _heading("采集源明细")
        _table(
            [
                [
                    s.id,
                    f"{s.source_type.value}/{s.identifier[:20]}",
                    s.cursor_message_id or "-",
                    s.backfill_cursor_id or "-",
                    "已补完" if s.backfill_done else "补历史中",
                    s.total_collected,
                    s.total_backfilled,
                    "启用" if s.enabled else "停用",
                ]
                for s in sources
            ],
            ["ID", "源", "高水位", "低水位", "回溯", "追新", "回溯数", "状态"],
        )


# --- run ---------------------------------------------------------------------


@app.command()
def run(
    extractor: Annotated[str, typer.Option(help="抽取器：rule / sheet / llm")] = "rule",
    limit: Annotated[
        int | None, typer.Option(help="最多解析多少条，默认不设上限、处理到清空为止")
    ] = None,
    concurrency: Annotated[
        int, typer.Option(help="解析阶段并发处理数，每个并发任务用独立数据库连接")
    ] = 20,
    skip_collect: Annotated[bool, typer.Option("--skip-collect", help="只解析，不采集")] = False,
) -> None:
    """一条龙：采集全部启用的源，再解析待处理文本。"""
    if not skip_collect:
        _heading("[1/2] 采集")
        collect(None)
        typer.echo()
    _heading("[2/2] 解析" if not skip_collect else "解析")
    parse(extractor=extractor, limit=limit, concurrency=concurrency, doc_id=None, force=False)


# --- worker ------------------------------------------------------------------


@app.command()
def worker(
    once: Annotated[bool, typer.Option("--once", help="只跑一轮就退出，不常驻")] = False,
    interval: Annotated[int | None, typer.Option(help="轮询间隔秒数，覆盖配置")] = None,
    lease: Annotated[int | None, typer.Option(help="任务租约秒数，覆盖配置")] = None,
    parse_batch: Annotated[
        int | None, typer.Option(help="解析阶段每批领取多少条（不是总量上限，跑到队列清空）")
    ] = None,
    verify_batch: Annotated[
        int | None, typer.Option(help="校验阶段每批领取多少条（不是总量上限，跑到队列清空）")
    ] = None,
    collect_batch: Annotated[
        int | None, typer.Option(help="采集阶段每批领取多少个源（不是总量上限，跑到队列清空）")
    ] = None,
    write_batch: Annotated[
        int | None, typer.Option(help="攒够多少条处理完的任务再提交一次，覆盖配置")
    ] = None,
    extractor: Annotated[str | None, typer.Option(help="强制抽取器，留空按源类型自动选")] = None,
    progress_interval: Annotated[
        int | None, typer.Option(help="心跳进度日志间隔秒数，<=0 关闭，覆盖配置")
    ] = None,
) -> None:
    """常驻后台 worker：周期性地采集、解析、校验。

    与 `run` 的区别是它带**租约**：多个 worker 可以同时跑同一个库，
    同一条任务不会被两个进程重复处理；进程崩了，租约过期后任务自动回到队列。
    `run` 没有这层保护，只适合手动跑一次。
    """

    from funflix.worker import Worker, progress_heartbeat

    settings = get_settings().model_copy(
        update={
            k: v
            for k, v in {
                "worker_poll_seconds": interval,
                "worker_lease_seconds": lease,
                "worker_parse_batch": parse_batch,
                "worker_verify_batch": verify_batch,
                "worker_collect_batch": collect_batch,
                "worker_write_batch": write_batch,
                "worker_extractor": extractor,
                "worker_progress_seconds": progress_interval,
            }.items()
            if v is not None
        }
    )
    instance = Worker(settings)

    if once:

        async def _one() -> Any:
            await instance.startup_check()
            async with progress_heartbeat(
                settings.worker_progress_seconds,
                on_tick=lambda line: _dim(f"进度：{line}"),
            ):
                return await instance.run_once()

        report = _run(_one)
        _table(
            [
                ["采集", report.collect.claimed, report.collect.succeeded, report.collect.failed],
                ["解析", report.parse.claimed, report.parse.succeeded, report.parse.failed],
                ["校验", report.verify.claimed, report.verify.succeeded, report.verify.failed],
            ],
            ["队列", "领取", "成功", "失败"],
        )
        reclaimed = report.collect.reclaimed + report.parse.reclaimed + report.verify.reclaimed
        if reclaimed:
            _warn(f"重捞了 {reclaimed} 条上次未收尾的任务")
        if report.idle:
            typer.echo("三条队列都没有到点的任务")
        else:
            _ok("一轮完成")
        return

    _dim(
        f"轮询 {settings.worker_poll_seconds}s，租约 {settings.worker_lease_seconds}s，"
        f"每批 采集{settings.worker_collect_batch}/"
        f"解析{settings.worker_parse_batch}/校验{settings.worker_verify_batch}"
        "（各阶段循环拉取直到清空），"
        f"每 {settings.worker_write_batch} 条提交一次"
    )
    _heading("worker 运行中，Ctrl-C 停止")
    try:
        _run(instance.run_forever)
    except KeyboardInterrupt:
        # asyncio.run 会把 KeyboardInterrupt 透上来，这里只是让它安静退出
        typer.echo()
        _ok("worker 已停止")


# --- db ----------------------------------------------------------------------


def _alembic_config():
    """定位 alembic 配置与迁移脚本。

    两种运行场景都要成立：

    - **装出来的包**：配置和 migrations/ 都在 `funflix/` 包内（见 pyproject 的
      force-include）。此时不能依赖当前工作目录 —— 用户在任何目录敲
      `funflix db upgrade` 都该能建库。
    - **源码仓库里开发**：包内没有这两个文件，回落到仓库根目录那份。

    `script_location` 一律显式覆盖成绝对路径：alembic.ini 里写的是相对路径
    `migrations`，它按 cwd 解析，装包场景下必然找不到。
    """
    import pathlib

    from alembic.config import Config

    pkg = pathlib.Path(__file__).resolve().parent
    packaged_ini = pkg / "alembic.ini"
    packaged_migrations = pkg / "migrations"

    if packaged_ini.is_file():
        cfg = Config(str(packaged_ini))
        if packaged_migrations.is_dir():
            cfg.set_main_option("script_location", str(packaged_migrations))
        return cfg

    # 源码仓库：包目录是 src/funflix，仓库根在它的上两级
    repo_root = pkg.parent.parent
    repo_ini = repo_root / "alembic.ini"
    if repo_ini.is_file():
        cfg = Config(str(repo_ini))
        cfg.set_main_option("script_location", str(repo_root / "migrations"))
        return cfg

    # 都找不到就按老行为走 cwd，让 alembic 自己报它的错
    return Config("alembic.ini")


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """执行数据库迁移。"""
    from alembic import command

    command.upgrade(_alembic_config(), revision)
    _ok(f"已迁移到 {revision}")


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument(help="目标版本，如 -1")]) -> None:
    """回滚迁移。"""
    from alembic import command

    command.downgrade(_alembic_config(), revision)
    _ok(f"已回滚到 {revision}")


@db_app.command("current")
def db_current() -> None:
    """显示当前数据库版本。"""
    from alembic import command

    command.current(_alembic_config(), verbose=True)


@db_app.command("revision")
def db_revision(message: Annotated[str, typer.Option("-m", "--message")]) -> None:
    """按模型变更自动生成迁移脚本。"""
    from alembic import command

    command.revision(_alembic_config(), message=message, autogenerate=True)


#: 重建时清空的数据表。顺序按外键依赖从下游到上游排，
#: 即便不用 CASCADE 也能安全删。
@db_app.command("reset")
def db_reset(
    keep_documents: Annotated[
        bool, typer.Option("--keep-documents", help="保留原始文本，只重建下游解析结果")
    ] = False,
    keep_cursors: Annotated[
        bool, typer.Option("--keep-cursors", help="保留采集水位（清空原始文本时不要用）")
    ] = False,
    purge_checks: Annotated[
        bool, typer.Option("--purge-checks", help="连校验历史（link_check）一起清空，默认保留")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """清空数据表并重建。采集源配置保留。

    默认会把采集水位一起归零 —— 原始文本被清空后若水位还留着，
    采集器会认为"都采过了"，重建后一条都拉不回来。

    校验历史（link_check）默认不清空——它按 (provider, share_id) 锚定身份，
    不依赖 resource 行；重新解析出同样身份的资源后，用 `funflix db
    relink-checks` 把历史接回来，不用重新探测一遍。真要连它一起清，加
    `--purge-checks`。
    """
    from funflix.base.db import session_scope
    from funflix.services.maintenance import data_tables, reset_pipeline_data

    tables = data_tables(keep_documents=keep_documents, purge_checks=purge_checks)
    _warn(f"将清空：{', '.join(tables)}")
    _warn("采集源配置保留" + ("，水位保留" if keep_cursors or keep_documents else "，采集水位归零"))
    _warn("校验历史清空" if purge_checks else "校验历史保留（可用 db relink-checks 重新接回）")
    if not yes and not typer.confirm("确认执行？此操作不可撤销"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await reset_pipeline_data(
                session,
                keep_documents=keep_documents,
                keep_cursors=keep_cursors,
                purge_checks=purge_checks,
            )

    report = _run(_do)
    _table(
        [[t, report.before[t], report.after[t]] for t in report.before],
        ["表", "重建前", "重建后"],
    )
    if keep_documents:
        _dim(f"已把 {report.documents_requeued} 条原始文本的解析状态重置为待解析")
    _ok("重建完成")


@db_app.command("retag")
def db_retag(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """按当前规则重新归类已有标签。

    标签维度的判定规则会迭代（比如题材白名单），但规则只影响**新建**的标签 ——
    改规则前存进去的行不会自己变。这条命令把历史数据补齐。

    同一个标签名在新旧维度下各有一行时会合并：关联迁到新行，旧行删除。
    """
    from funflix.base.db import session_scope
    from funflix.services.maintenance import retag_all

    if not yes and not typer.confirm("将重新归类全部标签并合并重复项，继续？"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await retag_all(session)

    report = _run(_do)
    _table(
        [
            ["标签总数", report.total],
            ["改了维度", report.moved],
            ["合并删除", report.merged],
            ["修正计数", report.recounted],
        ],
        ["项", "数量"],
    )
    _ok("标签重新归类完成")


@db_app.command("requeue")
def db_requeue(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """把「新支持的网盘」的历史资源放回校验队列。

    落库时还不支持的网盘会被写成 unsupported 且不排复查时间，之后即使加了
    探针也永远不会被领取 —— 新链接正常校验、老链接静默地一直停在 unsupported。
    加完探针跑一次这个。
    """
    from funflix.base.db import session_scope
    from funflix.services.maintenance import requeue_now_checkable
    from funflix.services.verify.registry import supported_providers

    _dim("当前可校验：" + ", ".join(p.value for p in supported_providers()))
    if not yes and not typer.confirm("将把这些网盘的 unsupported 资源改回待校验，继续？"):
        raise typer.Abort()

    async def _do() -> int:
        async with session_scope() as session:
            return await requeue_now_checkable(session)

    count = _run(_do)
    if count:
        _ok(f"已重新排队 {count} 条资源")
    else:
        typer.echo("没有需要重新排队的资源")


@db_app.command("cleanup-resources")
def db_cleanup_resources(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """重分类城通链接，并清除频道自宣、短链、资料页和播放页。"""
    from funflix.base.db import session_scope
    from funflix.services.maintenance import cleanup_resources

    if not yes and not typer.confirm("将合并重复城通资源并删除黑名单链接，继续？"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await cleanup_resources(session)

    report = _run(_do)
    _table(
        [
            ["扫描 other", report.other_scanned],
            ["识别城通", report.ctfile_found],
            ["重分类城通", report.ctfile_reclassified],
            ["合并重复", report.duplicates_merged],
            ["删除黑名单", report.blacklisted_deleted],
            ["重算作品", report.media_recounted],
        ],
        ["项", "数量"],
    )
    _ok("资源清理完成")


@db_app.command("relink-checks")
def db_relink_checks(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """用已有校验历史恢复重新解析后新建资源的校验状态。

    典型顺序：`db reset --keep-documents` 清空重建 resource → 重新跑一遍
    `parse` → 这条命令。`link_check` 跟 resource 没有外键，完全独立存储，
    只按 (provider, share_id) 找回每条链接最新一条历史，把新 resource 的
    check_status/last_checked_at/next_check_at 恢复回去，不用重新探测。
    """
    from funflix.base.db import session_scope
    from funflix.services.maintenance import relink_checks

    if not yes and not typer.confirm("将按 (provider, share_id) 恢复校验状态，继续？"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await relink_checks(session)

    report = _run(_do)
    _table([["恢复状态", report.hydrated]], ["项", "数量"])
    _ok("校验状态恢复完成")


_CanonKeyOption = Annotated[
    str | None,
    typer.Option("--key", help="只处理这一个作品归一键（series_norm_key），用于单组演练"),
]
#: 归一命令**默认 dry-run**。这几条命令会不可逆地删改几十万行 media，
#: 默认空跑、要写库必须显式 `--apply`，这样手滑敲错命令的后果是看一眼报告。
_CanonApplyOption = Annotated[
    bool, typer.Option("--apply", help="真正写库。不传则只统计和抽样（dry-run）")
]


def _samples_block(samples: list[str], heading: str) -> None:
    if not samples:
        return
    _heading(heading)
    for line in samples:
        _dim(f"  {line}")


@canon_app.command("purge")
def canon_purge(
    apply: _CanonApplyOption = False,
    key: _CanonKeyOption = None,
    limit: Annotated[int | None, typer.Option("--limit", help="最多删多少行，用于小步试探")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """删掉根本不是作品的 media 行（页面文案、提取码、分享 ID、磁力链、安装包）。

    resource 行**不删** —— 链接是真的，只是归属错了，删掉关联之后它们变成
    未归属资源。这一步碰不到 resource / link_check / raw_document。

    先空跑看报告，确认无误再加 `--apply`。
    """
    from funflix.base.db import session_scope
    from funflix.services.canon import purge_junk_media

    if apply and not yes and not typer.confirm("将**不可逆地删除**命中的 media 行，继续？"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await purge_junk_media(session, dry_run=not apply, key=key, limit=limit)

    report = _run(_do)
    _table(
        [
            ["扫描 media", report.scanned],
            ["判为垃圾", report.junk],
            ["实际删除", report.deleted],
            ["断开资源关联", report.links_detached],
            ["断开标签关联", report.tags_detached],
            ["修正标签计数", report.tags_recounted],
            ["重算 Work 计数", report.works_recounted],
        ],
        ["项", "数量"],
    )
    _samples_block(report.samples, "命中样例")
    if report.dry_run:
        _warn("dry-run：没有写库。确认报告无误后加 --apply 执行")
    else:
        _ok("垃圾行清理完成")


@canon_app.command("rebuild")
def canon_rebuild(
    apply: _CanonApplyOption = False,
    key: _CanonKeyOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """按当前规则重算归一键，建 Work，回填 media 归属，合并撞车的季。

    零 LLM 调用 —— 这一步靠规则就能搬掉绝大部分重复。跑完之后剩下的
    残局（同一部剧因为别名、演员列、季号歧义而没并上的）交给 `canon resolve`。

    建议顺序：先 `canon purge`，再这一条。垃圾行没清掉的话会凭空造出
    十几万个垃圾 Work（这里会跳过它们，但单组演练时容易看花眼）。
    """
    from funflix.base.db import session_scope
    from funflix.services.canon import rebuild_works

    if apply and not yes and not typer.confirm("将重算全部 media 的归属并合并重复季，继续？"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await rebuild_works(session, dry_run=not apply, key=key)

    report = _run(_do)
    _table(
        [
            ["扫描 media", report.scanned],
            ["跳过垃圾行", report.skipped_junk],
            ["跳过已裁决", report.skipped_decided],
            ["作品归一键", report.keys],
            ["新建 Work", report.works_created],
            ["复用 Work", report.works_existing],
            ["回填 media", report.media_updated],
            ["撞车的季", report.season_conflicts],
            ["合并删除 media", report.media_merged],
            ["迁移资源关联", report.links_moved],
            ["丢弃重复关联", report.links_dropped],
            ["换位腾挪", report.parked],
            ["重算 Work 计数", report.works_recounted],
        ],
        ["项", "数量"],
    )
    _samples_block(report.samples, "最大的归一组")
    if report.dry_run:
        _warn("dry-run：没有写库。确认报告无误后加 --apply 执行")
    else:
        _ok("归一键重算完成")


@canon_app.command("resolve")
def canon_resolve(
    apply: _CanonApplyOption = False,
    key: Annotated[
        str | None,
        typer.Option("--key", help="只处理这一个候选块（block_key），用于单组演练"),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="最多送多少个块，用于小额预算试探")
    ] = None,
    concurrency: Annotated[int, typer.Option("--concurrency", help="并发调用数")] = 8,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """把规则搞不定的残局送 LLM 裁决，结果写 `title_canon`。

    这一步**不动 media 一个字段** —— 只往 `title_canon` 写裁决。要落到库上得
    再跑 `canon merge`。分两步是故意的：裁决是花钱买来的，先落盘、再人工抽查，
    确认没有误并才应用。

    只送候选项 ≥2 的候选块（实测约 4,900 个）。单候选项的块没有可并的对象，
    送过去纯烧钱。已经是 `decided` 的键会被跳过，所以中断后重跑只打残局，
    不重复付费。

    每轮开头还会先免费沉淀一批：字面恰好等于某个**已知作品键**的未裁决键，
    答案已经在库里了，不必再问模型（见 `services/canon/sediment.py`）。
    """
    from funflix.base.db import session_scope
    from funflix.services.canon import resolve_canon

    if apply and not yes and not typer.confirm("将发起真实 LLM 调用并产生费用，继续？"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await resolve_canon(
                session,
                dry_run=not apply,
                key=key,
                limit=limit,
                concurrency=concurrency,
            )

    report = _run(_do)
    _table(
        [
            ["沉淀免费判掉", report.settled],
            ["扫描 media", report.scanned],
            ["候选块总数", report.blocks],
            ["够格送裁决", report.blocks_eligible],
            ["本次送出", report.blocks_sent],
            ["调用次数", report.calls],
            ["调用失败", report.calls_failed],
            ["写入裁决", report.decided],
            ["其中判为垃圾", report.junk],
            ["校验不通过", report.rejected],
            ["模型漏答", report.missing],
            ["输入 token", report.input_tokens],
            ["输出 token", report.output_tokens],
        ],
        ["项", "数量"],
    )
    _samples_block(report.samples, "待裁决的候选块（同块内的候选项）")
    if report.dry_run:
        _warn("dry-run：一次调用都没发。确认要送的块无误后加 --apply 执行")
    else:
        _ok("裁决完成，接下来用 canon merge 应用")


@canon_app.command("merge")
def canon_merge(
    apply: _CanonApplyOption = False,
    key: Annotated[
        str | None,
        typer.Option("--key", help="只应用这一条裁决（title_canon.norm_key）"),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """把 `title_canon` 里已裁决的结果落到 media / work 上。

    `is_junk` 的键走和 `canon purge` 完全一样的删除路径（resource 行保留）。
    其余键把 media 重挂到裁决出的 Work 上；裁决给了具体季号的覆盖季号，
    给 null 的**不动**规则逐行判出的季号 —— null 的意思是"这个键没锁定某一季"，
    不是"第 0 季"。
    """
    from funflix.base.db import session_scope
    from funflix.services.canon import apply_canon_decisions

    if apply and not yes and not typer.confirm("将按裁决重挂 media 并合并撞车的季，继续？"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await apply_canon_decisions(session, dry_run=not apply, key=key)

    report = _run(_do)
    _table(
        [
            ["已裁决条数", report.decisions],
            ["判为垃圾的键", report.junk_keys],
            ["删除 media", report.junk_media_deleted],
            ["断开资源关联", report.links_detached],
            ["新建 Work", report.works_created],
            ["复用 Work", report.works_existing],
            ["重挂 media", report.media_rehomed],
            ["覆盖季号", report.seasons_overridden],
            ["撞车的季", report.season_conflicts],
            ["合并删除 media", report.media_merged],
            ["迁移资源关联", report.links_moved],
            ["丢弃重复关联", report.links_dropped],
            ["换位腾挪", report.parked],
            ["重算 Work 计数", report.works_recounted],
        ],
        ["项", "数量"],
    )
    _samples_block(report.samples, "将要应用的裁决")
    if report.dry_run:
        _warn("dry-run：没有写库。确认报告无误后加 --apply 执行")
    else:
        _ok("裁决应用完成")


#: 修复命令和归一命令同一个口径：**默认 dry-run**。`scan` 虽然只写任务表、
#: 不碰 media，但默认空跑能让「规则改完先看一眼检出量」成为顺手的习惯 ——
#: 这一眼正是爆炸半径闸门之外的第二道人工防线。
_RepairApplyOption = Annotated[
    bool, typer.Option("--apply", help="真正写库。不传则只统计和抽样（dry-run）")
]
_RepairKeyOption = Annotated[
    str | None,
    typer.Option("--key", help="只处理这一个作品归一键（series_norm_key），用于单组演练"),
]


@repair_app.command("scan")
def repair_scan(
    apply: _RepairApplyOption = False,
    key: _RepairKeyOption = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="最多扫多少行 media，用于分轮磨完全表")
    ] = None,
) -> None:
    """按当前规则重扫 media，把需要修的行落成 `repair_task`。

    **只读 + 只写任务表** —— 一个 media 字段都不碰，所以可以每轮跑。规则没变时
    每一行都判为「不用修」，于是一个任务都不建、一个字都不写，这是它能挂在
    流水线上的前提。

    检出分三类：`retitle`（标题/类型/年份变了）、`rehome`（作品归属或季号变了，
    目标被占时就是**多合一**）、`delete`（垃圾行和零资源空壳）。真正改库是
    `repair apply` 的事 —— 分两步是故意的：检测便宜且可逆，应用不可逆。

    `--limit` 限的是**扫多少行**，不是建多少任务。一轮扫不完下一轮从头再扫，
    已有的 pending 任务靠部分唯一索引不会重复建。
    """
    from funflix.base.db import session_scope
    from funflix.services.repair import scan_media

    bar = tqdm(total=0, desc="扫描", unit="行", leave=False)

    def _on_progress(scanned: int) -> None:
        # 总数不预查：`select count(*)` 在 89 万行的表上要几秒，而这里只是
        # 个进度提示。直接把已扫数当成总数往前推。
        bar.total = max(scanned, bar.total or 0)
        bar.n = scanned
        bar.refresh()

    async def _do():
        async with session_scope() as session:
            return await scan_media(
                session, dry_run=not apply, key=key, limit=limit, on_progress=_on_progress
            )

    try:
        report = _run(_do)
    finally:
        bar.close()

    _table(
        [
            ["扫描 media", report.scanned],
            ["需要修复", report.planned],
            ["  改标题", report.retitle],
            ["  改归属", report.rehome],
            ["  删除", report.delete],
            ["其中会多合一", report.expect_merge],
            ["新建任务", report.created],
            ["刷新任务", report.refreshed],
            ["已有任务不变", report.unchanged],
            ["撤销旧任务", report.cancelled],
        ],
        ["项", "数量"],
    )
    _samples_block(report.samples, "检出样例")
    if report.dry_run:
        _warn("dry-run：连任务表都没写。确认检出量和样例无误后加 --apply")
    elif report.planned == 0:
        _ok("全库都符合当前规则，没有需要修的行")
    else:
        _ok(f"检出 {report.planned} 行，接下来用 repair apply 应用")


@repair_app.command("apply")
def repair_apply(
    apply: _RepairApplyOption = False,
    key: _RepairKeyOption = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="每一类最多领多少个任务，节流阀")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="跳过爆炸半径闸门（确认规则没写错再用）")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """把 `repair_task` 里的 pending 任务落到 media / work 上。

    **不可逆。** 两部剧并成一部之后没有任何信息能把它们分回去。所以默认
    dry-run，并且在动手前检查爆炸半径：删除任务超过 media 总量 5%、或重挂
    超过 20% 就直接拒绝 —— 一次规则改动不该动到全库两成，真触发了更可能是
    规则写错了。确认无误才用 `--force` 放行。

    执行顺序是硬性的 `delete → retitle → rehome`：先把垃圾清掉，免得被并进
    真作品；改标题不动身份，最安全；重挂放最后，由 `assign_identities` 一步
    完成移动、撞车合并、换位腾挪。`resource` 行全程保留 —— 链接是真的，
    只是归属错了。

    `--limit` 是节流阀，一轮刷不完下一轮接着刷，pending 任务天然是断点。
    """
    from funflix.base.db import session_scope
    from funflix.services.repair import BlastRadiusExceeded, apply_repairs

    if apply and not yes and not typer.confirm("将**不可逆地**删改 media 并合并重复，继续？"):
        raise typer.Abort()

    bar = tqdm(total=0, desc="重挂", unit="行", leave=False)

    def _on_progress(done: int) -> None:
        bar.total = max(done, bar.total or 0)
        bar.n = done
        bar.refresh()

    async def _do():
        async with session_scope() as session:
            return await apply_repairs(
                session,
                dry_run=not apply,
                key=key,
                limit=limit,
                force=force,
                on_progress=_on_progress,
            )

    try:
        report = _run(_do)
    except BlastRadiusExceeded as exc:
        _fail(f"爆炸半径闸门拦住了：{exc}")
    finally:
        bar.close()

    _table(
        [
            ["领取任务 删除", report.delete],
            ["领取任务 改标题", report.retitle],
            ["领取任务 改归属", report.rehome],
            ["实际删除 media", report.deleted],
            ["断开资源关联", report.links_detached],
            ["实际改标题", report.retitled],
            ["实际重挂", report.rehomed],
            ["新建 Work", report.works_created],
            ["复用 Work", report.works_existing],
            ["多合一删除 media", report.merged],
            ["迁移资源关联", report.links_moved],
            ["丢弃重复关联", report.links_dropped],
            ["换位腾挪", report.parked],
            ["重算 Work 计数", report.works_recounted],
            ["跳过（已无需修）", report.skipped],
            ["失败", report.failed],
        ],
        ["项", "数量"],
    )
    _samples_block(report.samples, "将要应用的任务")
    if report.dry_run:
        _warn("dry-run：没有写库。确认报告无误后加 --apply 执行")
    elif report.failed:
        _warn(f"{report.failed} 个任务失败，错误记在 repair_task.error 上")
    else:
        _ok("修复应用完成")


@repair_app.command("requeue")
def repair_requeue(
    apply: _RepairApplyOption = False,
    limit: Annotated[int | None, typer.Option("--limit", help="这一轮最多打回多少份文档")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """把解析规则版本过期的文档打回 parse 队列（深层修复）。

    浅层修复只能从已清洗的 `media.title` 重算，所以修不了两类情况：旧规则
    **洗坏**、信息已经丢了的行，以及抽取器**切分逻辑**本身变了的情况（一条
    分享该拆成几个作品项变了，这在 media 层面看不出来）。这两类只能回到
    `raw_document.content` 重解析。

    靠 `raw_document.parse_rules_version` 做预筛 —— 213 万份文档不可能每次
    改规则都全量重跑。改了抽取逻辑要手动 bump `PARSE_RULES_VERSION`
    （刻意不用源码哈希：改个注释不该让 213 万份文档重排队）。

    打回之后**什么都不用做** —— 现有 parse 节点本来就是个队列，会按它自己的
    `--limit` 节奏消化。只收 `parse_status = done` 的文档：pending / failed
    的本来就在队列里，碰它只会把退避时间清掉。
    """
    from funflix.base.db import session_scope
    from funflix.services.repair import requeue_stale_documents

    if apply and not yes and not typer.confirm("将把过期文档打回 pending 重新解析，继续？"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await requeue_stale_documents(session, dry_run=not apply, limit=limit)

    report = _run(_do)
    _table(
        [
            ["当前规则版本", report.version],
            ["版本过期文档", report.stale],
            ["本轮打回", report.requeued],
        ],
        ["项", "数量"],
    )
    if report.dry_run:
        _warn("dry-run：没有写库。确认数量无误后加 --apply 执行")
    elif report.stale > report.requeued:
        _ok(f"打回 {report.requeued} 份，还剩 {report.stale - report.requeued} 份下轮继续")
    else:
        _ok("过期文档已全部打回队列，等 parse 消化")


@db_app.command("info")
def db_info() -> None:
    """显示当前连接的数据库（只显示方言，URL 含密码不打印）。"""
    settings = get_settings()
    typer.echo(f"  方言    {settings.database_url.split('://', 1)[0]}")
    typer.echo(f"  SQLite  {settings.is_sqlite}")


# --- user ----------------------------------------------------------------------


@user_app.command("create")
def user_create(
    username: Annotated[str, typer.Argument(help="登录用户名")],
    password: Annotated[
        str | None,
        typer.Option("--password", help="不给的话会交互式输入（不回显）"),
    ] = None,
) -> None:
    """创建一个「运维」区登录账号。"""
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import User
    from funflix.security import hash_password

    if password is None:
        password = typer.prompt("密码", hide_input=True, confirmation_prompt=True)

    async def _do() -> bool:
        async with session_scope() as session:
            existing = await session.scalar(select(User).where(User.username == username))
            if existing is not None:
                return False
            session.add(User(username=username, password_hash=hash_password(password)))
            await session.commit()
            return True

    if not _run(_do):
        _fail(f"用户名已存在：{username}")
    _ok(f"已创建用户 {username}")


@user_app.command("set-password")
def user_set_password(
    username: Annotated[str, typer.Argument(help="登录用户名")],
    password: Annotated[
        str | None,
        typer.Option("--password", help="不给的话会交互式输入（不回显）"),
    ] = None,
) -> None:
    """重置某个账号的密码。"""
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import User
    from funflix.security import hash_password

    if password is None:
        password = typer.prompt("新密码", hide_input=True, confirmation_prompt=True)

    async def _do() -> bool:
        async with session_scope() as session:
            user = await session.scalar(select(User).where(User.username == username))
            if user is None:
                return False
            user.password_hash = hash_password(password)
            await session.commit()
            return True

    if not _run(_do):
        _fail(f"用户不存在：{username}")
    _ok(f"已重置 {username} 的密码")


@user_app.command("list")
def user_list() -> None:
    """列出全部账号。"""
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import User

    async def _do() -> list[User]:
        async with session_scope() as session:
            return list(await session.scalars(select(User).order_by(User.username)))

    users = _run(_do)
    if not users:
        typer.echo("暂无账号")
        return
    _table(
        [[u.username, "启用" if u.is_active else "已停用", u.created_at] for u in users],
        ["用户名", "状态", "创建时间"],
    )


def _set_active(username: str, *, active: bool) -> None:
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import User

    async def _do() -> bool:
        async with session_scope() as session:
            user = await session.scalar(select(User).where(User.username == username))
            if user is None:
                return False
            user.is_active = active
            await session.commit()
            return True

    if not _run(_do):
        _fail(f"用户不存在：{username}")
    _ok(f"已{'启用' if active else '停用'} {username}")


@user_app.command("enable")
def user_enable(username: Annotated[str, typer.Argument(help="登录用户名")]) -> None:
    """重新启用一个被停用的账号。"""
    _set_active(username, active=True)


@user_app.command("disable")
def user_disable(username: Annotated[str, typer.Argument(help="登录用户名")]) -> None:
    """停用一个账号（保留记录，只是不能再登录）。"""
    _set_active(username, active=False)


# --- sync ----------------------------------------------------------------------


def _print_sync_report(report: SyncReport) -> None:
    _table(
        [[t.table, t.fetched, t.applied, t.skipped_conflicts] for t in report.tables],
        ["表", "拉到", "应用", "冲突跳过"],
    )
    if report.total_skipped:
        _warn(f"共 {report.total_skipped} 行因业务唯一键冲突被跳过，详见日志")


def _resolve_sync_tables(job: str | None) -> tuple[str, ...] | None:
    """`--job` 给定时按 `JOB_TABLES` 收窄同步范围；不给时同步全部表（人工场景）。"""
    from funflix.services.sync import JOB_TABLES

    if job is None:
        return None
    try:
        return JOB_TABLES[job]
    except KeyError:
        raise typer.BadParameter(f"未知 job: {job!r}，可选值：{', '.join(JOB_TABLES)}") from None


async def _run_sync_direction(
    direction: Literal["pull", "push"], tables: tuple[str, ...] | None
) -> SyncReport:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from funflix.base.config import Settings
    from funflix.base.db import create_engine, session_scope
    from funflix.services import sync as sync_service

    remote_engine = create_engine(Settings(database_url=get_settings().remote_database_url))
    try:
        remote_maker = async_sessionmaker(remote_engine, expire_on_commit=False)
        async with session_scope() as local, remote_maker() as remote:
            action = sync_service.pull if direction == "pull" else sync_service.push
            return await action(local, remote, tables=tables)
    finally:
        await remote_engine.dispose()


_JobOption = Annotated[
    str | None,
    typer.Option(help="只同步该 job 需要的表（collect/parse/verify），不给则同步全部表"),
]


@sync_app.command("pull")
def sync_pull(job: _JobOption = None) -> None:
    """从远端库拉取变更到本地库（远端为准，last-write-wins）。"""
    tables = _resolve_sync_tables(job)

    async def _do() -> SyncReport:
        return await _run_sync_direction("pull", tables)

    report = _run(_do)
    _print_sync_report(report)
    _ok(f"拉取完成，共应用 {report.total_applied} 行")


@sync_app.command("push")
def sync_push(job: _JobOption = None) -> None:
    """把本地库的变更推送到远端库（按行 last-write-wins，冲突跳过不中断整批）。"""
    tables = _resolve_sync_tables(job)

    async def _do() -> SyncReport:
        return await _run_sync_direction("push", tables)

    report = _run(_do)
    _print_sync_report(report)
    _ok(f"推送完成，共应用 {report.total_applied} 行")


# --- source ------------------------------------------------------------------


async def _require_source(session, source_id: uuid.UUID):
    from funflix.models import Source

    source = await session.get(Source, source_id)
    if source is None:
        _fail(f"采集源 #{source_id} 不存在")
    return source


@source_app.command("add")
def source_add(
    url: Annotated[str, typer.Argument(help="采集源地址")],
    interval: Annotated[int, typer.Option(help="采集间隔（秒）")] = 900,
    max_pages: Annotated[int, typer.Option(help="单次采集最多翻几页")] = 5,
    cursor: Annotated[str | None, typer.Option(help="起始水位；留空则首次只取最新一页")] = None,
) -> None:
    """登记一个采集源。类型与标识按 URL 自动识别。"""
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import Source
    from funflix.services.collect.registry import detect_source, supported_source_types

    detected = detect_source(url)
    if detected is None:
        _fail(f"无法识别采集源: {url}\n当前支持：{[s.value for s in supported_source_types()]}")
    source_type, identifier = detected

    async def _do() -> tuple[uuid.UUID, bool]:
        async with session_scope() as session:
            existing = await session.scalar(
                select(Source).where(
                    Source.source_type == source_type, Source.identifier == identifier
                )
            )
            if existing is not None:
                return existing.id, False
            source = Source(
                source_type=source_type,
                url=url,
                identifier=identifier,
                fetch_interval_seconds=interval,
                max_pages_per_fetch=max_pages,
                cursor_message_id=cursor,
            )
            session.add(source)
            await session.commit()
            return source.id, True

    source_id, created = _run(_do)
    label = f"#{source_id} {source_type.value}/{identifier}"
    _ok(f"已登记 {label}") if created else _warn(f"采集源已存在 {label}")


@source_app.command("list")
def source_list() -> None:
    """列出全部采集源及其水位。"""
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import Source

    async def _do():
        async with session_scope() as session:
            return list(await session.scalars(select(Source).order_by(Source.id)))

    rows = _run(_do)
    if not rows:
        typer.echo("还没有采集源，用 `funflix source add <url>` 添加")
        return

    _table(
        [
            [
                s.id,
                s.source_type.value,
                s.identifier,
                s.cursor_message_id or "-",
                s.total_collected + s.total_backfilled,
                "启用" if s.enabled else "停用",
                f"失败{s.consecutive_failures}" if s.consecutive_failures else "",
            ]
            for s in rows
        ],
        ["ID", "类型", "标识", "水位", "累计产出", "状态", "异常"],
    )


@source_app.command("show")
def source_show(source_id: uuid.UUID) -> None:
    """查看采集源详情。"""
    from funflix.base.db import session_scope

    async def _do():
        async with session_scope() as session:
            return await _require_source(session, source_id)

    s = _run(_do)
    _heading(f"#{s.id} {s.source_type.value}/{s.identifier}")
    for label, value in [
        ("标题", s.title or "-"),
        ("地址", s.url),
        ("启用", s.enabled),
        ("采集间隔", f"{s.fetch_interval_seconds}s"),
        ("翻页上限", s.max_pages_per_fetch),
        ("水位", s.cursor_message_id or "-"),
        ("水位时间", s.cursor_published_at or "-"),
        ("最后采集", s.last_fetched_at or "-"),
        ("最后成功", s.last_success_at or "-"),
        ("下次采集", s.next_fetch_at or "-"),
        ("累计产出", s.total_collected + s.total_backfilled),
        ("追新产出", s.total_collected),
        ("回灌产出", s.total_backfilled),
        ("连续失败", s.consecutive_failures),
        ("最后错误", s.last_error or "-"),
        ("自定义状态", s.extra or "-"),
    ]:
        typer.echo(f"  {label:<10} {value}")


@source_app.command("set")
def source_set(
    source_id: uuid.UUID,
    interval: Annotated[int | None, typer.Option(help="采集间隔（秒）")] = None,
    max_pages: Annotated[int | None, typer.Option(help="单次翻页上限（追新方向）")] = None,
    cursor: Annotated[str | None, typer.Option(help="回拨水位即可重采历史")] = None,
    title: Annotated[str | None, typer.Option(help="展示名")] = None,
) -> None:
    """修改采集源配置。

    补历史（backfill）没有翻页上限，每次 collect 都会一口气扫到底。
    """
    from funflix.base.db import session_scope

    async def _do() -> None:
        async with session_scope() as session:
            source = await _require_source(session, source_id)
            if interval is not None:
                source.fetch_interval_seconds = interval
            if max_pages is not None:
                source.max_pages_per_fetch = max_pages
            if cursor is not None:
                source.cursor_message_id = cursor
            if title is not None:
                source.title = title
            await session.commit()

    _run(_do)
    _ok(f"已更新 #{source_id}")


@source_app.command("reset-cursor")
def source_reset_cursor(
    source_id: Annotated[uuid.UUID | None, typer.Argument(help="留空则重置全部采集源")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """只归零采集水位，已采集的原始文本原样保留。

    用于定期核查"水位往前走了，但对应内容其实没真正采集成功"这类漂移 ——
    水位清零后重新 `collect` 会把该源从头再翻一遍，旧内容会被
    `content_hash` 唯一约束挡掉（不会重复入库），只有真正漏采的部分才会
    补进来。连原始文本一起清空用 `funflix db reset`。
    """
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import Source

    target = f"#{source_id}" if source_id is not None else "全部采集源"
    if not yes and not typer.confirm(f"确认重置 {target} 的采集水位？已采文本不受影响"):
        raise typer.Abort()

    async def _do() -> int:
        async with session_scope() as session:
            if source_id is not None:
                sources = [await _require_source(session, source_id)]
            else:
                sources = list(await session.scalars(select(Source)))
            for source in sources:
                source.reset_watermark()
            await session.commit()
            return len(sources)

    count = _run(_do)
    _ok(f"已重置 {count} 个采集源的水位")


@source_app.command("enable")
def source_enable(source_id: uuid.UUID) -> None:
    """启用采集源。"""
    _toggle_source(source_id, True)


@source_app.command("disable")
def source_disable(source_id: uuid.UUID) -> None:
    """停用采集源。"""
    _toggle_source(source_id, False)


def _toggle_source(source_id: uuid.UUID, value: bool) -> None:
    from funflix.base.db import session_scope

    async def _do() -> None:
        async with session_scope() as session:
            source = await _require_source(session, source_id)
            source.enabled = value
            await session.commit()

    _run(_do)
    _ok(f"#{source_id} 已{'启用' if value else '停用'}")


@source_app.command("remove")
def source_remove(
    source_id: uuid.UUID,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """删除采集源。已采集的原始文本会保留。"""
    from funflix.base.db import session_scope

    if not yes and not typer.confirm(f"确认删除采集源 #{source_id}？已采文本会保留"):
        raise typer.Abort()

    async def _do() -> None:
        async with session_scope() as session:
            source = await _require_source(session, source_id)
            await session.delete(source)
            await session.commit()

    _run(_do)
    _ok(f"已删除 #{source_id}")


@source_app.command("types")
def source_types() -> None:
    """列出当前支持的采集源类型。"""
    from funflix.services.collect.registry import supported_source_types

    for source_type in supported_source_types():
        typer.echo(f"  {source_type.value}")
    _dim("新增类型 = 在 services/collect/ 下加一个采集器并注册")


@source_app.command("collect")
def source_collect(
    source_id: Annotated[uuid.UUID | None, typer.Argument(help="留空则采集全部启用的源")] = None,
) -> None:
    """立即采集一次。"""
    collect(source_id)


@app.command("collect")
def collect(
    source_id: Annotated[uuid.UUID | None, typer.Argument(help="留空则采集全部启用的源")] = None,
    batch_size: Annotated[int, typer.Option(help="内部每批拉取多少个源")] = 500,
    write_batch: Annotated[int, typer.Option(help="消费者每攒够几条落库结果批量提交一次")] = 100,
    flush_interval: Annotated[
        float, typer.Option(help="消费者最多攒这么多秒也强制提交一次，避免看起来卡住")
    ] = 10.0,
    concurrency: Annotated[
        int,
        typer.Option(help="处理单元并发线程数（并发抓 HTTP，不碰数据库），默认取 max(8, CPU 核数)"),
    ] = max(8, os.cpu_count() or 1),
    queue_maxsize: Annotated[
        int, typer.Option(help="待抓取任务队列的容量上限，塞满就阻塞生产者，避免抓太猛被限流")
    ] = 2000,
    limit: Annotated[
        int,
        typer.Option(
            help="这一次 collect 累计最多规划这么多个任务就收工（跟 --queue-maxsize"
            " 限的瞬时积压不是一回事），扫不到的源留给下次 collect。0 表示不限，一次扫完"
        ),
    ] = 2000,
) -> None:
    """采集：把源里的新内容写成原始文本。

    批量模式用 `services/collect/concurrent_runner.py` 的 funworker 流水线
    执行：一个生产者线程按 `--batch-size` 翻页读启用的源。Telegram 源的补
    历史因为消息 ID 单调递增、每页条数固定，不必等真的抓到内容就能靠整数
    运算把后续每一页的游标提前算出来，拆成多个可并发抓取的翻页任务；其余
    情况（Telegram 追新、腾讯文档）当一个不透明的整源任务，内部翻页原样在
    处理单元线程里跑完。所有任务不分源、不分类型，全部丢进同一条队列，由
    `--concurrency` 个处理单元线程并发抓取，一个消费者线程每攒够
    `--write-batch` 条或每 `--flush-interval` 秒批量落库、回写水位一次——
    消费者永远只有一个线程，逐条提交会让落库耗时线性堆在这一个线程上，抵消
    掉抓取侧的并发收益。水位在规划阶段就乐观提交，不等对应任务真的被消费——
    中途异常顶多丢掉几个还没来得及抓的页面，content_hash 唯一约束保证不会
    重复入库，每周的水位重置也会把整个源重新刷一遍，可接受。

    进度条不再按"处理了百分之几"算——一个源可能被拆成几十个并发任务，
    "整体百分比"这个概念本身就不成立了，改成展示队列的入队数、以及消费者
    真正提交成功的条数，跟着任务被拆分、并发消化、批量落库的真实节奏走。
    """
    from funflix.services.collect.concurrent_runner import run_collect_pipeline

    bar = tqdm(total=0, desc="采集", unit="任务", leave=False)
    bar.set_postfix_str(f"{concurrency} 线程")

    def _on_progress(total: int, done: int) -> None:
        # total 随生产者规划出更多任务动态增长，done 不会超过它——min() 只是
        # 防个万一（比如两次回调之间 total 还没来得及反映最新一次入队）。
        bar.total = total
        bar.n = min(done, total)
        bar.refresh()

    try:
        result = run_collect_pipeline(
            source_id=source_id,
            batch_size=batch_size,
            write_batch=write_batch,
            flush_interval=flush_interval,
            concurrency=concurrency,
            queue_maxsize=queue_maxsize,
            limit=limit or None,
            on_progress=_on_progress,
        )
    finally:
        bar.close()

    if not result.reports and not result.backfill_pages.pages:
        typer.echo("没有启用的采集源")
        return

    for identifier, report in result.reports:
        if not report.ok:
            typer.secho(f"[{identifier}] 采集失败: {report.error}", fg=typer.colors.RED)
            continue
        _ok(
            f"[{identifier}] 拉取 {report.fetched} → 新增 {report.created} / "
            f"重复 {report.duplicated} / 空 {report.skipped_empty}"
            f"，水位 {report.cursor_before or '-'} → {report.cursor_after or '-'}"
            + ("（未取完）" if report.truncated else "")
        )

    pages = result.backfill_pages
    if pages.pages:
        # 并发翻页任务分散在多个源里，没有一个自然的时间点能拼出"某个源
        # 的补历史完整报告"，只按全局汇总展示，跟按源展示的整源任务报告
        # 分开。
        _ok(
            f"补历史（并发翻页 {pages.pages} 页）→ 新增 {pages.created} / "
            f"重复 {pages.duplicated} / 空 {pages.skipped}"
        )


# --- parse -------------------------------------------------------------------


@app.command()
def parse(
    extractor: Annotated[
        str | None,
        typer.Option(help="抽取器：rule / sheet / llm。留空则按来源类型自动选"),
    ] = None,
    limit: Annotated[
        int | None, typer.Option(help="最多解析多少条，默认不设上限、处理到清空为止")
    ] = None,
    batch_size: Annotated[int, typer.Option(help="内部每批拉取多少条")] = 500,
    write_batch: Annotated[
        int, typer.Option(help="每批内部按此粒度批量预读去重键、处理完再提交一次")
    ] = 20,
    concurrency: Annotated[
        int,
        typer.Option(
            help="处理单元并发线程数（并发跑 extract()，不碰数据库），默认取 max(8, CPU 核数)"
        ),
    ] = max(8, os.cpu_count() or 1),
    doc_id: Annotated[uuid.UUID | None, typer.Option(help="只解析指定文档")] = None,
    force: Annotated[bool, typer.Option(help="忽略缓存，强制重新抽取")] = False,
    shard: Annotated[
        str | None,
        typer.Option(help="分片并行，写成 i/N（如 0/8）：只处理 id 末位落在第 i 片的文档"),
    ] = None,
) -> None:
    """抽取：把原始文本解析成作品与资源。

    不指定抽取器时按来源类型自动选：表格源用 sheet，自由文本用 rule。
    用错抽取器不会报错，只会静默地大批归属失败，所以默认按源类型分开。

    默认不设总量上限——待处理的文档会一直处理到清空为止。批量模式用
    `services/extract/concurrent_runner.py` 的 funworker 流水线执行：一个生产者
    线程按 `--batch-size` 翻页读文档，`--concurrency` 个处理单元线程并发跑
    `extract()`（通常是耗时的网络/LLM 调用），一个消费者线程每攒够
    `--write-batch` 条就批量预读去重键、落库、提交一次。两条文档若抽出同一部
    作品会撞库唯一约束，静默回滚重试、不计入失败次数，属预期行为。

    库在远端时（本机到阿里云 RDS 实测往返 131ms）瓶颈是网络往返而不是 CPU，
    调大 `--concurrency` 不会提速——落库全程只有一个消费者线程。这时用
    `--shard i/N` 开 N 个进程并行推同一个队列，各片按 id 末位取模互斥，
    不重不漏。切漏了的后果只是那些文档留在 pending，补跑一遍不带
    `--shard` 的即可收干净。
    """
    from funflix.base.db import session_scope
    from funflix.models import RawDocument
    from funflix.services.extract.concurrent_runner import (
        count_pending,
        parse_shard,
        run_parse_pipeline,
    )
    from funflix.services.extract.registry import (
        default_extractor_for,
        get_extractor,
        supported_extractors,
    )
    from funflix.services.extract.runner import parse_document

    shard_spec: tuple[int, int] | None = None
    if shard is not None:
        try:
            shard_spec = parse_shard(shard)
        except ValueError as exc:
            _fail(str(exc))

    if extractor is not None:
        try:
            get_extractor(extractor)
        except ValueError as exc:
            _fail(str(exc))
        except Exception as exc:
            # LLM 抽取器在构造时就要读凭证，缺配置在这里先报错，
            # 不然要等流水线的生产者线程里才炸、且会被误当成"没有数据"重试到天荒地老。
            _fail(f"抽取器 {extractor!r} 初始化失败：{exc}\n可选抽取器：{supported_extractors()}")

    if doc_id is not None:

        async def _do_single() -> list[Any]:
            async with session_scope() as session:
                doc = await session.get(RawDocument, doc_id)
                if doc is None:
                    _fail(f"文档 #{doc_id} 不存在")
                kind = extractor or default_extractor_for(doc.source_type)
                impl = get_extractor(kind)
                report = await parse_document(session, doc, impl, force=force)
                await session.commit()
                return [report]

        reports = _run(_do_single)
    else:

        async def _count() -> int:
            async with session_scope() as session:
                return await count_pending(session, limit=limit, shard=shard_spec)

        total = _run(_count)
        if not total:
            typer.echo("没有待解析的文档")
            return

        # 分母不用启动前查出来的待处理数固定住——批量模式跑起来可能持续好一阵，
        # 期间会有新文档变成待处理（比如 collect 还在并发写入）。改成跟着
        # 生产者实际入队的条数动态长，分子是消费者已处理（成功或失败）的条数，
        # 两者都是流水线的真实累计计数，不是启动前的一次性快照。
        desc = "解析" if shard_spec is None else f"解析[{shard_spec[0]}/{shard_spec[1]}]"
        bar = tqdm(total=0, desc=desc, unit="条", leave=False, disable=total <= 1)
        bar.set_postfix_str(f"{concurrency} 线程")

        def _on_progress(total: int, done: int) -> None:
            bar.total = total
            bar.n = min(done, total)
            bar.refresh()

        try:
            reports = run_parse_pipeline(
                extractor_name=extractor,
                limit=limit,
                batch_size=batch_size,
                write_batch=write_batch,
                concurrency=concurrency,
                force=force,
                shard=shard_spec,
                on_progress=_on_progress,
            )
        finally:
            bar.close()

    if not reports:
        typer.echo("没有待解析的文档")
        return

    good = [r for r in reports if r.ok]
    failed = [r for r in reports if not r.ok]
    _dim(f"抽取器 {extractor or '自动（按来源类型选择）'}")
    typer.secho(
        f"解析 {len(reports)} 条：成功 {len(good)}  失败 {len(failed)}  "
        f"目录帖 {sum(1 for r in good if r.is_catalog)}  "
        f"缓存命中 {sum(1 for r in good if r.from_cache)}\n"
        f"  作品 新建 {sum(r.media_created for r in good)} / "
        f"复用 {sum(r.media_reused for r in good)}\n"
        f"  资源 新建 {sum(r.resources_created for r in good)} / "
        f"更新 {sum(r.resources_updated for r in good)}  "
        f"未归属 {sum(r.unattributed_links for r in good)}",
        fg=typer.colors.GREEN if not failed else typer.colors.YELLOW,
    )
    for r in failed[:5]:
        typer.secho(f"  #{r.document_id} 失败: {r.error}", fg=typer.colors.RED)


@app.command()
def verify(
    limit: Annotated[
        int | None, typer.Option(help="最多校验多少条，默认不设上限、处理到清空为止")
    ] = None,
    batch_size: Annotated[int, typer.Option(help="内部每批拉取多少条")] = 500,
    write_batch: Annotated[int, typer.Option(help="消费者每攒够几条批量落库、提交一次")] = 20,
    concurrency: Annotated[
        int,
        typer.Option(help="处理单元并发线程数（并发探测，不碰数据库），默认取 max(8, CPU 核数)"),
    ] = max(8, os.cpu_count() or 1),
    resource_id: Annotated[uuid.UUID | None, typer.Option(help="只校验指定资源")] = None,
    rate: Annotated[
        float, typer.Option(help="每个网盘每秒最多几次请求（个别网盘另有更慢的下调值）")
    ] = 5.0,
    recheck_all: Annotated[
        bool, typer.Option("--recheck-all", help="忽略复查时间，重校验全部可校验资源")
    ] = False,
) -> None:
    """校验：探测网盘链接现在还能不能用。

    默认不设总量上限——待校验的资源会一直处理到清空为止。批量模式用
    `services/verify/concurrent_runner.py` 的 funworker 流水线执行：一个生产者
    线程按 `--batch-size` 翻页读资源，`--concurrency` 个处理单元线程并发跑
    `probe.check()`，一个消费者线程每攒够 `--write-batch` 条就批量落库、
    提交一次。默认限速已提到 5/秒——打太快可能触发网盘风控，被限流的响应会
    误判成链接失效；这是接受该风险换取速度的选择，见 README。

    `--rate` 是全局值，扛不住它的网盘由 `services/verify/runner.py` 的
    `PROVIDER_RATE_LIMITS` 单独下调（目前只有阿里云盘，实测数据在那里）。
    """
    from funflix.base.db import session_scope
    from funflix.models import Resource
    from funflix.services.verify.concurrent_runner import count_due, run_verify_pipeline
    from funflix.services.verify.registry import assert_registry_matches_enum, get_probe
    from funflix.services.verify.runner import RateLimiter, check_resource

    assert_registry_matches_enum()

    if resource_id is not None:

        async def _do_single() -> list[Any]:
            async with session_scope() as session:
                target = await session.get(Resource, resource_id)
                if target is None:
                    _fail(f"资源 #{resource_id} 不存在")
                probe = get_probe(target.provider)
                limiter = RateLimiter(rate_per_second=rate)
                try:
                    report = await check_resource(session, target, probe, limiter)
                finally:
                    if probe is not None:
                        aclose = getattr(probe, "aclose", None)
                        if aclose is not None:
                            await aclose()
                await session.commit()
                return [report]

        reports = _run(_do_single)
    else:

        async def _count() -> int:
            async with session_scope() as session:
                return await count_due(session, recheck_all=recheck_all, limit=limit)

        total = _run(_count)
        if not total:
            typer.echo("没有待校验的资源")
            return

        # 分母不用启动前查出来的待校验数固定住，理由同 parse：不设 --limit 时
        # 批量模式会跑到清空为止，期间水位会变化。改成跟着生产者实际入队的
        # 条数动态长，分子是消费者已处理（成功或失败）的条数。
        bar = tqdm(total=0, desc="校验", unit="条", leave=False, disable=total <= 1)
        bar.set_postfix_str(f"{concurrency} 线程")

        def _on_progress(total: int, done: int) -> None:
            bar.total = total
            bar.n = min(done, total)
            bar.refresh()

        try:
            reports = run_verify_pipeline(
                limit=limit,
                batch_size=batch_size,
                write_batch=write_batch,
                concurrency=concurrency,
                rate=rate,
                recheck_all=recheck_all,
                on_progress=_on_progress,
            )
        finally:
            bar.close()

    if not reports:
        typer.echo("没有待校验的资源")
        return

    from collections import Counter

    counts = Counter(r.status.value for r in reports)
    changed = sum(1 for r in reports if r.changed)
    _table(
        [[status, n] for status, n in counts.most_common()],
        ["结论", "条数"],
    )
    _ok(f"校验 {len(reports)} 条，状态变化 {changed} 条")
    errors = [r for r in reports if r.status in {CheckStatus.ERROR, CheckStatus.RATE_LIMITED}]
    if errors:
        _warn(f"有 {len(errors)} 条判不出结论（探针异常或被限流），已排退避重试，不会被误判为失效")
        for r in errors[:3]:
            _dim(f"  #{r.resource_id} {r.status.value}: {r.detail}")


@app.command()
def probes() -> None:
    """列出可用的网盘校验探针。"""
    from funflix.services.verify.registry import supported_providers

    for provider in supported_providers():
        from funflix.services.verify.registry import get_probe

        probe = get_probe(provider)
        auth = "需登录" if probe and probe.needs_auth else "匿名"
        typer.echo(f"  {provider.value:<8} {probe.name if probe else '-':<16} {auth}")
    _dim("其余网盘入库但不校验（check_status=unsupported）")


@app.command()
def extractors() -> None:
    """列出可用的抽取器。"""
    from funflix.services.extract.registry import supported_extractors

    notes = {
        "rule": "规则抽取，免费离线，自由文本的降级路径",
        "sheet": "表格行抽取，按列直接映射，零猜测零 token",
        "llm": "大模型抽取，质量最高，凭证走 funsecret",
    }
    for name in supported_extractors():
        typer.echo(f"  {name:<8} {notes.get(name, '')}")


# --- 查询 --------------------------------------------------------------------


#: 每季最多列出多少条资源链接。热门剧会被很多频道反复分享（`大主宰` 第 2 季
#: 有 793 条），全列出来是几百屏翻不完的噪声，而使用者要的是「一条能用的链接」。
#: 真实总数看季行上的 `resource_count`。
DEFAULT_SEASON_LINKS = 5

#: 季号为 0 时的展示名，见 `models/media.py` 的 `NO_SEASON`。
_NO_SEASON_LABEL = "正片"


async def _search_work_rows(
    keyword: str,
    *,
    limit: int = 20,
    media_type: MediaType | None = None,
    year: int | None = None,
    valid_only: bool = False,
    links: int = DEFAULT_SEASON_LINKS,
) -> tuple[str, list[Any], dict[uuid.UUID, list[Any]], int]:
    """搜作品，连带每季的前几条资源。

    返回 `(后端名, 作品列表, 季 id → 资源列表, 匹配总数)`。资源单独放在字典里而不是挂
    在季对象上：数量是截断的，赋给关系属性会被 ORM 当成「这就是全部关联」，
    flush 时把没列进来的关联行删掉 —— 截断展示会变成截断数据
    （`funflix-api` 那边用 `set_committed_value` 绕开同一个坑）。
    """
    from sqlalchemy import case

    from funflix.base.db import session_scope
    from funflix.models import Resource, media_resource
    from funflix.services.search import SearchQuery, count_works, get_backend, search_works

    async with session_scope() as session:
        backend = get_backend(session)
        query = SearchQuery(
            keyword=keyword,
            media_type=media_type,
            year=year,
            valid_only=valid_only,
            limit=limit,
            with_seasons=True,
        )
        rows = await search_works(session, query)
        total = await count_works(session, query)

        resources: dict[uuid.UUID, list[Any]] = {}
        if links > 0:
            # 逐季带 LIMIT 查，而不是一条 IN 查完再在内存里切 —— 后者会把
            # 那 793 条全读回来才扔掉。季数不多（一页 20 部作品也就几十季），
            # 多几十次带索引的小查询比搬一遍关联表便宜。
            for work in rows:
                for season in work.seasons:
                    if season.resource_count == 0:
                        continue
                    stmt = (
                        select(Resource)
                        .join(media_resource, media_resource.c.resource_id == Resource.id)
                        .where(media_resource.c.media_id == season.id)
                    )
                    if valid_only:
                        stmt = stmt.where(Resource.check_status == CheckStatus.VALID)
                    # 可用的排前面，其余按入库倒序
                    stmt = stmt.order_by(
                        case((Resource.check_status == CheckStatus.VALID, 0), else_=1),
                        Resource.id.desc(),
                    ).limit(links)
                    resources[season.id] = list(await session.scalars(stmt))
        return backend.name, rows, resources, total


def _print_work_detail(
    work: Any,
    resources: dict[uuid.UUID, list[Any]],
    *,
    valid_only: bool = False,
) -> None:
    """打印一部作品及其季列表。

    季下面的资源条数是截断的（见 `DEFAULT_SEASON_LINKS`），所以同时把季行上
    的 `resource_count` 打出来 —— 那才是真实总数。
    """
    year = f" ({work.year})" if work.year else ""
    counts = f"{work.season_count} 季 / {work.resource_count} 资源"
    if work.valid_resource_count:
        counts += f"（{work.valid_resource_count} 可用）"
    _heading(f"#{work.id} {work.title}{year}  [{work.media_type.value}]  {counts}")
    if work.aliases:
        _dim("  别名: " + "、".join(work.aliases))
    if not work.seasons:
        _dim("    （无季）")
        return

    for season in work.seasons:
        label = _NO_SEASON_LABEL if season.season == 0 else f"第{season.season}季"
        total = season.valid_resource_count if valid_only else season.resource_count
        typer.echo(f"  {label}  {season.title}  （{total} 资源）")
        rows = resources.get(season.id, [])
        for r in rows:
            passcode = f"  提取码 {r.passcode}" if r.passcode else ""
            typer.echo(f"    [{r.provider.value:<7}] {r.check_status.value:<11} {r.url}{passcode}")
        if total > len(rows):
            _dim(f"    …… 另有 {total - len(rows)} 条未列出")


@app.command()
def search(
    keyword: Annotated[str, typer.Argument(help="剧名关键词")],
    limit: Annotated[int, typer.Option(help="最多返回多少部作品")] = 20,
    media_type: Annotated[
        MediaType | None, typer.Option(help="按类型筛选；传 book/comic/other 才看得到非影视")
    ] = None,
    year: Annotated[int | None, typer.Option(help="按年份筛选")] = None,
    valid_only: Annotated[bool, typer.Option("--valid-only", help="只看校验通过的资源")] = False,
    links: Annotated[
        int, typer.Option(help="每季最多列出多少条链接；0 = 只看季列表")
    ] = DEFAULT_SEASON_LINKS,
) -> None:
    """按剧名搜索作品，按季列出资源。

    一部剧一条，季是子层 —— 搜「大主宰」给的是一条「大主宰（4 季）」，
    不是 448 条同名行。

    PostgreSQL 上走 pg_trgm 模糊匹配并按相似度排序，其余方言回落到 LIKE。
    默认只返回影视类型，小说/漫画要显式 `--media-type book`。
    """
    backend_name, rows, resources, total = _run(
        lambda: _search_work_rows(
            keyword,
            limit=limit,
            media_type=media_type,
            year=year,
            valid_only=valid_only,
            links=links,
        )
    )
    _dim(f"搜索后端 {backend_name}")
    if not rows:
        typer.echo(f"没有匹配 {keyword!r} 的作品")
        return

    for work in rows:
        _print_work_detail(work, resources, valid_only=valid_only)
    if total > len(rows):
        _dim(f"\n共 {total} 部作品，已显示前 {len(rows)} 部（--limit 调整）")


@app.command("doc")
def show_doc(doc_id: uuid.UUID) -> None:
    """查看一条原始文本及其解析状态。"""
    from funflix.base.db import session_scope
    from funflix.models import RawDocument

    async def _do():
        async with session_scope() as session:
            doc = await session.get(RawDocument, doc_id)
            if doc is None:
                _fail(f"文档 #{doc_id} 不存在")
            return doc

    doc = _run(_do)
    _heading(f"#{doc.id}  {doc.source_type.value}/{doc.source_name or '-'}")
    for label, value in [
        ("来源链接", doc.source_url or "-"),
        ("源侧消息", doc.source_msg_id or "-"),
        ("发布时间", doc.published_at or "-"),
        ("采集时间", doc.collected_at),
        ("解析状态", doc.parse_status.value),
        ("重试次数", doc.parse_attempts),
        ("最后错误", doc.parse_error or "-"),
        ("指纹", doc.content_hash[:16] + "…"),
    ]:
        typer.echo(f"  {label:<10} {value}")
    _dim("\n--- 原文 ---")
    typer.echo(doc.content)


# --- ingest ------------------------------------------------------------------


@app.command("ingest")
def ingest(
    path: Annotated[Path, typer.Argument(help="待导入文件：.txt 单条 / .jsonl 每行一条")],
    source_type: SourceType = SourceType.MANUAL,
    source_name: Annotated[str | None, typer.Option(help="来源名称")] = None,
    separator: Annotated[
        str | None, typer.Option(help="txt 的条目分隔符；不传则整个文件作为一条")
    ] = None,
) -> None:
    """从文件导入原始文本。"""
    from funflix.base.db import session_scope
    from funflix.schemas.raw import RawDocumentCreate
    from funflix.services.ingest import ingest_many

    if not path.exists():
        _fail(f"文件不存在: {path}")

    text = path.read_text(encoding="utf-8")
    payloads: list[RawDocumentCreate] = []

    if path.suffix == ".jsonl":
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                payloads.append(RawDocumentCreate.model_validate(json.loads(line)))
            except Exception as exc:
                _warn(f"第 {lineno} 行解析失败，已跳过: {exc}")
    else:
        chunks = text.split(separator) if separator else [text]
        payloads = [
            RawDocumentCreate(content=c, source_type=source_type, source_name=source_name)
            for c in (chunk.strip() for chunk in chunks)
            if c
        ]

    if not payloads:
        _fail("没有可导入的内容")

    async def _do() -> tuple[int, int]:
        async with session_scope() as session:
            outcomes = await ingest_many(session, payloads)
            await session.commit()
            dup = sum(1 for o in outcomes if o.duplicated)
            return len(outcomes) - dup, dup

    created, duplicated = _run(_do)
    _ok(f"导入完成：新增 {created} 条，重复跳过 {duplicated} 条")


# --- 交互式菜单 ----------------------------------------------------------------
#
# 全量覆盖：菜单项直接从 Typer/Click 的命令树里反射出来（`db`/`source` 这些子
# 分组也会递归进去），不用给每条命令另外手写一遍参数收集逻辑——新增命令、改
# 选项都不需要再回来同步这里。`search` 是唯一的例外：它有单独一套"关键词 ->
# 结果列表 -> 选一条看详情"的体验，比"逐个参数问一遍再原样拼回命令行"更顺手，
# 所以在分发时特判掉，其余命令一律走通用的“列参数、挨个问、拼成 argv、交给
# Click 自己解析校验”这条路径。


def _interactive_search() -> None:
    while True:
        keyword = typer.prompt(
            "请输入搜索关键词（直接回车返回菜单）", default="", show_default=False
        ).strip()
        if not keyword:
            return

        backend_name, rows, resources, total = _run(lambda kw=keyword: _search_work_rows(kw))
        _dim(
            f"搜索后端 {backend_name}"
            + (f"，共 {total} 部，显示前 {len(rows)} 部" if total > len(rows) else "")
        )
        if not rows:
            typer.echo(f"没有匹配 {keyword!r} 的作品")
            continue

        typer.echo()
        headers = ["作品", "类型", "季", "资源", "有效"]
        table_rows: list[list[Any]] = []
        for work in rows:
            year = f" ({work.year})" if work.year else ""
            # 计数读 Work 上的反规范化字段，不是数 `resources` 字典 ——
            # 那里面的条数是截断的（见 `DEFAULT_SEASON_LINKS`）。
            table_rows.append(
                [
                    f"{work.title}{year}",
                    work.media_type.value,
                    work.season_count,
                    work.resource_count,
                    work.valid_resource_count,
                ]
            )
        widths = [
            max([_width(headers[i])] + [_width(str(row[i])) for row in table_rows])
            for i in range(len(headers))
        ]

        def render(values: list[Any], widths: list[int] = widths) -> str:
            """把一行单元格按列宽补齐成一行；`widths` 用默认参数绑定当前轮的列宽。"""
            return "  ".join(
                str(v) + " " * (widths[i] - _width(str(v))) for i, v in enumerate(values)
            )

        _dim(render(headers))
        select_choices = [
            questionary.Choice(title=render(row), value=i) for i, row in enumerate(table_rows)
        ]
        select_choices.append(questionary.Choice(title="重新搜索", value=None))

        while True:
            choice = questionary.select("选择要查看的作品：", choices=select_choices).ask()
            if choice is None:
                break
            typer.echo()
            _print_work_detail(rows[choice], resources)
            typer.echo()


def _prompt_param_tokens(param: click.Parameter) -> list[str]:
    """为一个 click 参数交互式问值，返回要拼进 argv 的 token；留空且非必填就返回
    空列表，让 Click 自己套用默认值——不用在这儿重复一遍每个命令的默认值。"""
    label = param.opts[0] if isinstance(param, click.Option) else param.name
    help_text = (getattr(param, "help", None) or "").strip()
    hint = f"  ({help_text})" if help_text else ""

    if isinstance(param, click.Option) and param.is_flag:
        default_bool = bool(param.default)
        answer = typer.confirm(f"{label}{hint}", default=default_bool)
        if answer == default_bool:
            return []
        if answer and param.opts:
            return [param.opts[0]]
        if not answer and getattr(param, "secondary_opts", None):
            return [param.secondary_opts[0]]
        return []

    required = bool(getattr(param, "required", False))

    choices = getattr(param.type, "choices", None)
    if choices:
        # 有限选项集直接上下箭头选，不用记住/敲对拼写
        select_choices = [questionary.Choice(title=c, value=c) for c in choices]
        if not required:
            default_hint = f" {param.default}" if param.default not in (None, ...) else ""
            select_choices.append(
                questionary.Choice(title=f"（跳过，用默认{default_hint}）", value=None)
            )
        answer = questionary.select(f"{label}{hint}", choices=select_choices).ask()
        if answer is None:
            if required:
                raise click.Abort()
            return []
        return [answer] if isinstance(param, click.Argument) else [param.opts[0], answer]

    default_display = "" if param.default in (None, ...) else f" [默认 {param.default}]"
    while True:
        raw = typer.prompt(
            f"{label}{hint}{default_display}", default="", show_default=False
        ).strip()
        if raw:
            break
        if required:
            _warn("必填，不能留空")
            continue
        return []

    return [raw] if isinstance(param, click.Argument) else [param.opts[0], raw]


def _run_leaf_command(cmd: click.Command, qualified_name: str) -> None:
    try:
        argv: list[str] = []
        for param in cmd.params:
            if isinstance(param, click.Option) and "--help" in param.opts:
                continue
            argv.extend(_prompt_param_tokens(param))

        typer.echo()
        cmd.main(args=argv, prog_name=f"funflix {qualified_name}", standalone_mode=False)
    except (click.exceptions.Exit, typer.Exit, SystemExit):
        pass
    except (click.Abort, typer.Abort, KeyboardInterrupt):
        typer.echo()
        _warn("已取消")
    except click.ClickException as exc:
        exc.show()


def _menu_loop(group: click.Group, *, path: list[str]) -> None:
    while True:
        typer.echo()
        select_choices = []
        for name, sub in group.commands.items():
            desc = (sub.help or sub.short_help or "").strip().splitlines()[0] if sub.help else ""
            title = f"{name}  {desc}" if desc else name
            select_choices.append(questionary.Choice(title=title, value=name))
        select_choices.append(
            questionary.Choice(title="退出" if not path else "返回上级", value=None)
        )

        message = "请选择操作：" if not path else f"{' '.join(path)} 下的操作："
        name = questionary.select(message, choices=select_choices).ask()
        if name is None:
            return

        sub = group.commands[name]
        qualified = [*path, name]

        if not path and name == "search":
            _interactive_search()
            continue
        if isinstance(sub, click.Group):
            _menu_loop(sub, path=qualified)
            continue
        _run_leaf_command(sub, " ".join(qualified))


def _interactive_menu() -> None:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    try:
        _menu_loop(root, path=[])
    except (click.Abort, typer.Abort, KeyboardInterrupt):
        typer.echo()


# --- 供 status 之外的引用 ------------------------------------------------------

__all__ = ["app"]


if __name__ == "__main__":
    app()
