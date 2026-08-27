"""funflix 命令行入口。

覆盖整条流水线：采集 → 抽取 → 查询，以及数据库与状态检查。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from sqlalchemy import select
from tqdm import tqdm

from funflix.base.config import get_settings
from funflix.base.enums import CheckStatus, MediaType, ParseStatus, SourceType

logger = logging.getLogger(__name__)

app = typer.Typer(help="funflix 命令行工具", no_args_is_help=True)
db_app = typer.Typer(help="数据库迁移与检查", no_args_is_help=True)
source_app = typer.Typer(help="采集源管理与采集", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(source_app, name="source")

# --- 输出 helpers ------------------------------------------------------------


def _run[T](factory: Callable[[], Awaitable[T]]) -> T:
    """跑一个协程。每条命令都是一次性进程，不需要复用事件循环。"""
    return asyncio.run(factory())


async def _process_concurrently[T](
    items: list[T],
    handle: Callable[[Any, T], Awaitable[Any]],
    *,
    concurrency: int,
) -> list[Any]:
    """有界并发处理一批条目，`parse`/`verify` 复用。

    每个任务用**独立 session**（同一个 AsyncSession 不能被多个协程同时用），
    独立提交自己的事务——用独立 session 本身就决定了提交粒度只能是
    "每条一提交"，而不是整批共用一次提交，这是并发换来的代价。

    `handle` 拿到的 session 是这个任务自己的，不能用外层查出来的 ORM 对象
    （跨 session 用会出问题），要在 `handle` 内部按 id 重新加载——所以
    `items` 传的是 id，不是 ORM 对象。

    某个任务异常不会让其余任务陪葬：`asyncio.gather(..., return_exceptions=True)`
    保证一个任务的异常只体现为返回列表里的一个 `Exception` 实例。
    """
    from funflix.base.db import session_scope

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(item: T) -> Any:
        async with sem, session_scope() as session:
            try:
                result = await handle(session, item)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise

    return await asyncio.gather(*(_one(item) for item in items), return_exceptions=True)


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
    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max([_width(headers[i])] + [_width(row[i]) for row in cells]) for i in range(len(headers))
    ]

    def render(values: list[str]) -> str:
        return "  ".join(v + " " * (widths[i] - _width(v)) for i, v in enumerate(values))

    _dim(render(headers))
    for row in cells:
        typer.echo(render(row))


def _progress(items: list[Any], desc: str, unit: str) -> Any:
    """统一的进度条。只有多于一项时才显示，避免单条任务被进度条刷屏。"""
    return tqdm(items, desc=desc, unit=unit, leave=False, disable=len(items) <= 1)


def _progress_ticker(prefix: str) -> Callable[[str], None]:
    """`progress_heartbeat` 的 on_tick：用 `tqdm.write` 而不是 print，
    避免心跳行把正在刷新的 tqdm 进度条截断/错位。
    """

    def _on_tick(line: str) -> None:
        tqdm.write(typer.style(f"{prefix} 进度：{line}", fg=typer.colors.BRIGHT_BLACK))

    return _on_tick


def _progress_interval(override: int | None) -> int:
    return get_settings().worker_progress_seconds if override is None else override


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
    import logging

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
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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


# --- serve -------------------------------------------------------------------


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="代码变更自动重载（开发用）")] = False,
) -> None:
    """启动 API 服务。"""
    import uvicorn

    uvicorn.run(
        "funflix.api.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level=get_settings().log_level.lower(),
    )


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
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """清空数据表并重建。采集源配置保留。

    默认会把采集水位一起归零 —— 原始文本被清空后若水位还留着，
    采集器会认为"都采过了"，重建后一条都拉不回来。
    """
    from funflix.base.db import session_scope
    from funflix.services.maintenance import data_tables, reset_pipeline_data

    tables = data_tables(keep_documents=keep_documents)
    _warn(f"将清空：{', '.join(tables)}")
    _warn("采集源配置保留" + ("，水位保留" if keep_cursors or keep_documents else "，采集水位归零"))
    if not yes and not typer.confirm("确认执行？此操作不可撤销"):
        raise typer.Abort()

    async def _do():
        async with session_scope() as session:
            return await reset_pipeline_data(
                session, keep_documents=keep_documents, keep_cursors=keep_cursors
            )

    report = _run(_do)
    _table(
        [[t, report.before[t], report.after[t]] for t in report.before],
        ["表", "重建前", "重建后"],
    )
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


@db_app.command("info")
def db_info() -> None:
    """显示当前连接的数据库（只显示方言，URL 含密码不打印）。"""
    settings = get_settings()
    typer.echo(f"  方言    {settings.database_url.split('://', 1)[0]}")
    typer.echo(f"  SQLite  {settings.is_sqlite}")


# --- source ------------------------------------------------------------------


async def _require_source(session, source_id: int):
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

    async def _do() -> tuple[int, bool]:
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
                s.total_collected,
                "启用" if s.enabled else "停用",
                f"失败{s.consecutive_failures}" if s.consecutive_failures else "",
            ]
            for s in rows
        ],
        ["ID", "类型", "标识", "水位", "已采集", "状态", "异常"],
    )


@source_app.command("show")
def source_show(source_id: int) -> None:
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
        ("累计产出", s.total_collected),
        ("连续失败", s.consecutive_failures),
        ("最后错误", s.last_error or "-"),
        ("自定义状态", s.extra or "-"),
    ]:
        typer.echo(f"  {label:<10} {value}")


@source_app.command("set")
def source_set(
    source_id: int,
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


@source_app.command("enable")
def source_enable(source_id: int) -> None:
    """启用采集源。"""
    _toggle_source(source_id, True)


@source_app.command("disable")
def source_disable(source_id: int) -> None:
    """停用采集源。"""
    _toggle_source(source_id, False)


def _toggle_source(source_id: int, value: bool) -> None:
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
    source_id: int,
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
    source_id: Annotated[int | None, typer.Argument(help="留空则采集全部启用的源")] = None,
) -> None:
    """立即采集一次。"""
    collect(source_id)


@app.command("collect")
def collect(
    source_id: Annotated[int | None, typer.Argument(help="留空则采集全部启用的源")] = None,
    batch_size: Annotated[int, typer.Option(help="内部每批拉取多少个源")] = 500,
    write_batch: Annotated[int, typer.Option(help="攒够多少个源处理完再提交一次")] = 100,
    progress_interval: Annotated[
        int | None, typer.Option(help="进度心跳间隔秒数，<=0 关闭，默认取配置")
    ] = None,
) -> None:
    """采集：把源里的新内容写成原始文本。

    按 `--batch-size` 分批拉取启用的源（不会一次性把全部源读进内存），每处理完
    `--write-batch` 个源提交一次，而不是全部跑完才提交一次 —— 源很多时中途
    失败不会把已经采完的全部丢掉；也不需要等全部源跑完才落库。
    """
    from sqlalchemy import func, select

    from funflix.base.db import session_scope
    from funflix.models import Source
    from funflix.services.collect.runner import collect_source
    from funflix.worker import progress_heartbeat

    async def _do() -> list:
        interval = _progress_interval(progress_interval)
        async with (
            progress_heartbeat(interval, on_tick=_progress_ticker("采集")),
            session_scope() as session,
        ):
            if source_id is not None:
                total = 1
            else:
                total = int(
                    await session.scalar(
                        select(func.count()).select_from(Source).where(Source.enabled)
                    )
                    or 0
                )
            if not total:
                return []

            reports: list[Any] = []
            bar = tqdm(total=total, desc="采集", unit="源", leave=False, disable=total <= 1)
            pending = 0
            last_id = 0
            try:
                while True:
                    if source_id is not None:
                        # 单源模式没有分页可言，只在第一轮取一次。
                        chunk = [await _require_source(session, source_id)] if last_id == 0 else []
                    else:
                        # 按 id 游标翻页，理由同 `parse` 命令：已处理的源状态
                        # 未必总能把自己排除出 WHERE，游标才保证不会死循环。
                        chunk = list(
                            await session.scalars(
                                select(Source)
                                .where(Source.enabled, Source.id > last_id)
                                .order_by(Source.id)
                                .limit(batch_size)
                            )
                        )
                    if not chunk:
                        break

                    for target in chunk:
                        # 一个源可能翻上百页、跑几分钟。源级进度条只会停在那里不动，
                        # 分不清是在正常翻页还是卡死了 —— 所以把页级进度实时打出来。
                        label = target.identifier[:18]
                        inner = tqdm(
                            total=None, desc=f"  ↳ {label}", unit="页", leave=False, position=1
                        )

                        def _tick(p, _bar=inner) -> None:
                            _bar.total = p.budget or None
                            _bar.n = p.pages
                            stage = "追新" if p.stage == "fetch" else "补历史"
                            where = f" @{p.position}" if p.position else ""
                            extra = f" {p.detail}" if p.detail else ""
                            _bar.set_postfix_str(f"{stage} {p.messages}条{where}{extra}")
                            _bar.refresh()

                        bar.set_postfix_str(label)
                        try:
                            report = await collect_source(session, target, on_progress=_tick)
                        finally:
                            inner.close()
                        reports.append((target.identifier, report))
                        bar.update(1)
                        pending += 1
                        if pending >= write_batch:
                            await session.commit()
                            pending = 0
                    last_id = chunk[-1].id
                if pending:
                    await session.commit()
            finally:
                bar.close()
            return reports

    reports = _run(_do)
    if not reports:
        typer.echo("没有启用的采集源")
        return

    for identifier, report in reports:
        if not report.ok:
            typer.secho(f"[{identifier}] 采集失败: {report.error}", fg=typer.colors.RED)
            continue
        _ok(
            f"[{identifier}] 拉取 {report.fetched} → 新增 {report.created} / "
            f"重复 {report.duplicated} / 空 {report.skipped_empty}"
            f"，水位 {report.cursor_before or '-'} → {report.cursor_after or '-'}"
            + ("（未取完）" if report.truncated else "")
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
    concurrency: Annotated[int, typer.Option(help="并发处理数，每个并发任务用独立数据库连接")] = 20,
    doc_id: Annotated[int | None, typer.Option(help="只解析指定文档")] = None,
    force: Annotated[bool, typer.Option(help="忽略缓存，强制重新抽取")] = False,
    progress_interval: Annotated[
        int | None, typer.Option(help="进度心跳间隔秒数，<=0 关闭，默认取配置")
    ] = None,
) -> None:
    """抽取：把原始文本解析成作品与资源。

    不指定抽取器时按来源类型自动选：表格源用 sheet，自由文本用 rule。
    用错抽取器不会报错，只会静默地大批归属失败，所以默认按源类型分开。

    默认不设总量上限——待处理的文档会一直处理到清空为止，内部按 `--batch-size`
    分批拉取（不会一次性把全部待处理行都读进内存），每批内部用 `--concurrency`
    个并发任务处理（每个任务独立数据库连接与事务）。并发下两条文档若抽出
    同一部作品会撞库唯一约束，静默回滚重试、不计入失败次数，属预期行为。

    `--concurrency` 默认比 `verify` 高（20 而非 8）——parse 没有 `verify` 那种
    "打太快触发风控"的顾虑，瓶颈纯粹是远程数据库的往返延迟，多开并发只会重叠
    延迟、不会有副作用，上限只需留在数据库连接池容量（`pool_size=10` +
    `max_overflow=20`＝30）以内。
    """
    from sqlalchemy import func, or_, select

    from funflix.base.db import session_scope
    from funflix.models import RawDocument, utcnow
    from funflix.services.extract.registry import (
        default_extractor_for,
        get_extractor,
        supported_extractors,
    )
    from funflix.services.extract.runner import parse_document
    from funflix.worker import progress_heartbeat

    _cache: dict[str, Any] = {}

    def _extractor_for(doc: Any) -> Any:
        kind = extractor or default_extractor_for(doc.source_type)
        if kind not in _cache:
            try:
                _cache[kind] = get_extractor(kind)
            except ValueError as exc:
                _fail(str(exc))
            except Exception as exc:
                # LLM 抽取器在构造时就要读凭证，缺配置会在这里失败
                _fail(f"抽取器 {kind!r} 初始化失败：{exc}\n可选抽取器：{supported_extractors()}")
        return _cache[kind]

    async def _do() -> list:
        interval = _progress_interval(progress_interval)
        async with (
            progress_heartbeat(interval, on_tick=_progress_ticker("解析")),
            session_scope() as session,
        ):
            if doc_id is not None:
                doc = await session.get(RawDocument, doc_id)
                if doc is None:
                    _fail(f"文档 #{doc_id} 不存在")
                report = await parse_document(session, doc, _extractor_for(doc), force=force)
                await session.commit()
                return [report]

            now = utcnow()
            conditions = (
                RawDocument.parse_status == ParseStatus.PENDING,
                or_(
                    RawDocument.next_parse_at.is_(None),
                    RawDocument.next_parse_at <= now,
                ),
            )
            total = int(
                await session.scalar(
                    select(func.count()).select_from(RawDocument).where(*conditions)
                )
                or 0
            )
            if limit is not None:
                total = min(total, limit)
            if not total:
                return []

            reports: list[Any] = []
            remaining = total
            last_id = 0
            bar = tqdm(total=total, desc="解析", unit="条", leave=False, disable=total <= 1)
            try:
                while remaining > 0:
                    fetch = min(batch_size, remaining)
                    # 按 id 游标翻页，而不是复用同一个 WHERE 反复查——已处理的
                    # 行状态变化未必总能把自己排除出条件（比如未来加了强制
                    # 全量重跑之类的开关），游标才能保证不会在同一批里死循环。
                    docs = list(
                        await session.scalars(
                            select(RawDocument)
                            .where(*conditions, RawDocument.id > last_id)
                            .order_by(RawDocument.id)
                            .limit(fetch)
                        )
                    )
                    if not docs:
                        break
                    doc_ids = [doc.id for doc in docs]
                    # 抽取器解析在主协程里做（不在并发任务内）——`_extractor_for`
                    # 缺配置时会调用 `_fail()` 直接退出整个命令，必须在第一条
                    # 命中缺失抽取器时就同步炸出来，不能被并发任务的
                    # try/except 吞成"这一条失败"。
                    impls = {doc.id: _extractor_for(doc) for doc in docs}

                    async def _handle(
                        sub_session: Any, doc_id: int, *, impls: dict[int, Any] = impls
                    ) -> Any:
                        sub_doc = await sub_session.get(RawDocument, doc_id)
                        report = await parse_document(
                            sub_session, sub_doc, impls[doc_id], force=force
                        )
                        bar.update(1)
                        return report

                    results = await _process_concurrently(doc_ids, _handle, concurrency=concurrency)
                    for r in results:
                        if isinstance(r, Exception):
                            # 不应该发生——parse_document 自己兜底所有异常；
                            # 能漏到这里的只有 commit/rollback 本身的意外失败。
                            logger.exception("解析任务异常", exc_info=r)
                            continue
                        reports.append(r)
                    last_id = docs[-1].id
                    remaining -= len(docs)
            finally:
                bar.close()
            return reports

    reports = _run(_do)
    if not reports:
        typer.echo("没有待解析的文档")
        return

    good = [r for r in reports if r.ok]
    failed = [r for r in reports if not r.ok]
    _dim("抽取器 " + ", ".join(f"{e.name}/{e.version}" for e in _cache.values()))
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
    concurrency: Annotated[int, typer.Option(help="并发处理数，每个并发任务用独立数据库连接")] = 8,
    resource_id: Annotated[int | None, typer.Option(help="只校验指定资源")] = None,
    rate: Annotated[float, typer.Option(help="每个网盘每秒最多几次请求")] = 5.0,
    recheck_all: Annotated[
        bool, typer.Option("--recheck-all", help="忽略复查时间，重校验全部可校验资源")
    ] = False,
    progress_interval: Annotated[
        int | None, typer.Option(help="进度心跳间隔秒数，<=0 关闭，默认取配置")
    ] = None,
) -> None:
    """校验：探测网盘链接现在还能不能用。

    默认不设总量上限——待校验的资源会一直处理到清空为止，内部按 `--batch-size`
    分批拉取（不会一次性把全部待校验行都读进内存），每批内部用 `--concurrency`
    个并发任务处理。默认限速已提到 5/秒——打太快可能触发网盘风控，被限流的
    响应会误判成链接失效；这是接受该风险换取速度的选择，见 README。
    """
    from sqlalchemy import func, or_, select

    from funflix.base.db import session_scope
    from funflix.base.enums import CHECKABLE_PROVIDERS
    from funflix.models import Resource, utcnow
    from funflix.services.verify.registry import assert_registry_matches_enum, get_probe
    from funflix.services.verify.runner import RateLimiter, check_resource
    from funflix.worker import progress_heartbeat

    assert_registry_matches_enum()
    limiter = RateLimiter(rate_per_second=rate)
    probes: dict[Any, Any] = {}

    async def _do() -> list:
        interval = _progress_interval(progress_interval)
        async with (
            progress_heartbeat(interval, on_tick=_progress_ticker("校验")),
            session_scope() as session,
        ):
            if resource_id is not None:
                target = await session.get(Resource, resource_id)
                if target is None:
                    _fail(f"资源 #{resource_id} 不存在")
                probe = probes.setdefault(target.provider, get_probe(target.provider))
                report = await check_resource(session, target, probe, limiter)
                await session.commit()
                return [report]

            now = utcnow()
            conditions = [Resource.provider.in_(CHECKABLE_PROVIDERS)]
            if not recheck_all:
                conditions.append(
                    or_(
                        Resource.next_check_at.is_(None)
                        & (Resource.check_status == CheckStatus.UNCHECKED),
                        Resource.next_check_at <= now,
                    )
                )

            total = int(
                await session.scalar(select(func.count()).select_from(Resource).where(*conditions))
                or 0
            )
            if limit is not None:
                total = min(total, limit)
            if not total:
                return []

            reports: list[Any] = []
            remaining = total
            last_id = 0
            bar = tqdm(total=total, desc="校验", unit="条", leave=False, disable=total <= 1)
            try:
                while remaining > 0:
                    fetch = min(batch_size, remaining)
                    # 按 id 游标翻页：`--recheck-all` 下 WHERE 条件本身不会
                    # 因为处理过就自动排除已处理的行（只看 provider，跟校验
                    # 状态无关），不用游标会在同一批里死循环重复捞回同一批行。
                    rows = list(
                        await session.scalars(
                            select(Resource)
                            .where(*conditions, Resource.id > last_id)
                            .order_by(Resource.id)
                            .limit(fetch)
                        )
                    )
                    if not rows:
                        break
                    resource_ids = [row.id for row in rows]

                    async def _handle(sub_session: Any, res_id: int) -> Any:
                        sub_row = await sub_session.get(Resource, res_id)
                        probe = probes.setdefault(sub_row.provider, get_probe(sub_row.provider))
                        report = await check_resource(sub_session, sub_row, probe, limiter)
                        bar.update(1)
                        return report

                    results = await _process_concurrently(
                        resource_ids, _handle, concurrency=concurrency
                    )
                    for r in results:
                        if isinstance(r, Exception):
                            logger.exception("校验任务异常", exc_info=r)
                            continue
                        reports.append(r)
                    last_id = rows[-1].id
                    remaining -= len(rows)
            finally:
                bar.close()
            return reports

    reports = _run(_do)
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


@app.command()
def search(
    keyword: Annotated[str, typer.Argument(help="剧名关键词")],
    limit: Annotated[int, typer.Option(help="最多返回多少部作品")] = 20,
    media_type: Annotated[MediaType | None, typer.Option(help="按类型筛选")] = None,
    year: Annotated[int | None, typer.Option(help="按年份筛选")] = None,
    valid_only: Annotated[bool, typer.Option("--valid-only", help="只看校验通过的资源")] = False,
) -> None:
    """按剧名搜索作品及其资源。

    PostgreSQL 上走 pg_trgm 模糊匹配并按相似度排序，其余方言回落到 LIKE。
    """
    from sqlalchemy.orm import selectinload

    from funflix.base.db import session_scope
    from funflix.models import Media
    from funflix.services.search import SearchQuery, get_backend, search_media

    async def _do():
        async with session_scope() as session:
            backend = get_backend(session)
            rows = await search_media(
                session,
                SearchQuery(
                    keyword=keyword,
                    media_type=media_type,
                    year=year,
                    valid_only=valid_only,
                    limit=limit,
                ),
            )
            if rows:
                # 预加载资源，避免逐条访问时触发异步上下文外的懒加载
                await session.execute(
                    select(Media)
                    .options(selectinload(Media.resources))
                    .where(Media.id.in_([m.id for m in rows]))
                )
            return backend.name, rows

    backend_name, rows = _run(_do)
    _dim(f"搜索后端 {backend_name}")
    if not rows:
        typer.echo(f"没有匹配 {keyword!r} 的作品")
        return

    for media in rows:
        year = f" ({media.year})" if media.year else ""
        _heading(f"#{media.id} {media.title}{year}  [{media.media_type.value}]")
        resources = [
            r for r in media.resources if not valid_only or r.check_status is CheckStatus.VALID
        ]
        if not resources:
            _dim("    （无资源）")
            continue
        for r in resources:
            passcode = f"  提取码 {r.passcode}" if r.passcode else ""
            typer.echo(f"    [{r.provider.value:<7}] {r.check_status.value:<11} {r.url}{passcode}")


@app.command("doc")
def show_doc(doc_id: int) -> None:
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


# --- 供 status 之外的引用 ------------------------------------------------------

__all__ = ["app"]


if __name__ == "__main__":
    app()
