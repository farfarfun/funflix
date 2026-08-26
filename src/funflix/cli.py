"""funflix 命令行入口。

覆盖整条流水线：采集 → 抽取 → 查询，以及数据库与状态检查。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import select
from tqdm import tqdm

from funflix.base.config import get_settings
from funflix.base.enums import CheckStatus, MediaType, ParseStatus, SourceType

app = typer.Typer(help="funflix 命令行工具", no_args_is_help=True)
db_app = typer.Typer(help="数据库迁移与检查", no_args_is_help=True)
source_app = typer.Typer(help="采集源管理与采集", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(source_app, name="source")

# --- 输出 helpers ------------------------------------------------------------


def _run[T](factory: Callable[[], Awaitable[T]]) -> T:
    """跑一个协程。每条命令都是一次性进程，不需要复用事件循环。"""
    return asyncio.run(factory())


def _fail(message: str) -> None:
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


# --- status ------------------------------------------------------------------


@app.command()
def status(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="展开采集源明细")] = False,
) -> None:
    """查看流水线各环节的记录数。"""
    from sqlalchemy import func, select

    from funflix.base.db import session_scope
    from funflix.models import (
        Extraction,
        LinkCheck,
        Media,
        RawDocument,
        Resource,
        Source,
        media_resource,
    )

    async def _fetch() -> dict:
        async with session_scope() as session:

            async def count(model, *conditions) -> int:
                stmt = select(func.count()).select_from(model)
                if conditions:
                    stmt = stmt.where(*conditions)
                return await session.scalar(stmt) or 0

            async def group(model, column) -> dict:
                rows = await session.execute(
                    select(column, func.count()).select_from(model).group_by(column)
                )
                return dict(rows.all())

            return {
                "sources_total": await count(Source),
                "sources_enabled": await count(Source, Source.enabled),
                "sources_failing": await count(Source, Source.consecutive_failures > 0),
                "sources": list(await session.scalars(select(Source).order_by(Source.id))),
                "raw_total": await count(RawDocument),
                "raw_by_status": await group(RawDocument, RawDocument.parse_status),
                "extraction_total": await count(Extraction),
                "extraction_by_extractor": await group(Extraction, Extraction.model),
                "media_total": await count(Media),
                "media_by_type": await group(Media, Media.media_type),
                "resource_total": await count(Resource),
                "resource_by_check": await group(Resource, Resource.check_status),
                "resource_by_provider": await group(Resource, Resource.provider),
                # 未归属 = 在关联表里没有任何作品指向它
                "resource_orphan": await count(
                    Resource,
                    ~select(media_resource.c.resource_id)
                    .where(media_resource.c.resource_id == Resource.id)
                    .exists(),
                ),
                "media_resource_total": await count(media_resource),
                "check_total": await count(LinkCheck),
            }

    data = _run(_fetch)
    _dim(f"数据库 {get_settings().database_url.split('://', 1)[0]}\n")

    def section(index: int, name: str, total: int, note: str = "") -> None:
        _heading(f"{index}. {name}")
        typer.echo(f"   总数 {total}" + (f"   {note}" if note else ""))

    def breakdown(counts: dict, highlight: set[str] | None = None) -> None:
        if not counts:
            _dim("   （无记录）")
            return
        for key, value in sorted(counts.items(), key=lambda kv: -kv[1]):
            label = getattr(key, "value", str(key))
            color = typer.colors.YELLOW if highlight and label in highlight else None
            typer.secho(f"     {label:<14} {value}", fg=color)

    section(
        1,
        "采集源 source",
        data["sources_total"],
        f"启用 {data['sources_enabled']}   连续失败 {data['sources_failing']}",
    )
    if verbose and data["sources"]:
        for s in data["sources"]:
            typer.echo(
                f"     #{s.id} {s.source_type.value}/{s.identifier}  "
                f"水位 {s.cursor_message_id or '-'}  已采 {s.total_collected}  "
                f"{'启用' if s.enabled else '停用'}"
            )
    typer.echo()

    section(2, "原始文本 raw_document", data["raw_total"])
    breakdown(data["raw_by_status"], {ParseStatus.FAILED.value, ParseStatus.PENDING.value})
    typer.echo()

    section(3, "抽取留档 extraction", data["extraction_total"])
    breakdown(data["extraction_by_extractor"])
    typer.echo()

    section(4, "作品 media", data["media_total"])
    breakdown(data["media_by_type"])
    typer.echo()

    section(
        5,
        "资源 resource",
        data["resource_total"],
        f"未归属作品 {data['resource_orphan']}   作品↔资源关联 {data['media_resource_total']}",
    )
    breakdown(data["resource_by_check"], {CheckStatus.INVALID.value, CheckStatus.ERROR.value})
    _dim("   按网盘：")
    breakdown(data["resource_by_provider"])
    typer.echo()

    section(6, "校验历史 link_check", data["check_total"])


# --- run ---------------------------------------------------------------------


@app.command()
def run(
    extractor: Annotated[str, typer.Option(help="抽取器：rule / sheet / llm")] = "rule",
    limit: Annotated[int, typer.Option(help="本轮最多解析多少条")] = 200,
    skip_collect: Annotated[bool, typer.Option("--skip-collect", help="只解析，不采集")] = False,
) -> None:
    """一条龙：采集全部启用的源，再解析待处理文本。"""
    if not skip_collect:
        _heading("[1/2] 采集")
        collect(None)
        typer.echo()
    _heading("[2/2] 解析" if not skip_collect else "解析")
    parse(extractor=extractor, limit=limit, doc_id=None, force=False)


# --- worker ------------------------------------------------------------------


@app.command()
def worker(
    once: Annotated[bool, typer.Option("--once", help="只跑一轮就退出，不常驻")] = False,
    interval: Annotated[int | None, typer.Option(help="轮询间隔秒数，覆盖配置")] = None,
    lease: Annotated[int | None, typer.Option(help="任务租约秒数，覆盖配置")] = None,
    parse_batch: Annotated[int | None, typer.Option(help="每轮解析多少条")] = None,
    verify_batch: Annotated[int | None, typer.Option(help="每轮校验多少条")] = None,
    collect_batch: Annotated[int | None, typer.Option(help="每轮采集多少个源")] = None,
    extractor: Annotated[str | None, typer.Option(help="强制抽取器，留空按源类型自动选")] = None,
) -> None:
    """常驻后台 worker：周期性地采集、解析、校验。

    与 `run` 的区别是它带**租约**：多个 worker 可以同时跑同一个库，
    同一条任务不会被两个进程重复处理；进程崩了，租约过期后任务自动回到队列。
    `run` 没有这层保护，只适合手动跑一次。
    """
    from funflix.worker import Worker

    settings = get_settings().model_copy(
        update={
            k: v
            for k, v in {
                "worker_poll_seconds": interval,
                "worker_lease_seconds": lease,
                "worker_parse_batch": parse_batch,
                "worker_verify_batch": verify_batch,
                "worker_collect_batch": collect_batch,
                "worker_extractor": extractor,
            }.items()
            if v is not None
        }
    )

    instance = Worker(settings)

    if once:

        async def _one() -> Any:
            await instance.startup_check()
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
        f"批次 采集{settings.worker_collect_batch}/"
        f"解析{settings.worker_parse_batch}/校验{settings.worker_verify_batch}"
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
    from alembic.config import Config

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
_DATA_TABLES = ("media_resource", "link_check", "extraction", "resource", "media", "raw_document")


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
    from sqlalchemy import select, text

    from funflix.base.db import session_scope
    from funflix.models import Source

    tables = [t for t in _DATA_TABLES if not (keep_documents and t == "raw_document")]

    if keep_documents and not keep_cursors:
        # 原始文本还在，水位归零只会导致重复采集后被 content_hash 挡掉，无意义
        keep_cursors = True

    _warn(f"将清空：{', '.join(tables)}")
    _warn("采集源配置保留" + ("，水位保留" if keep_cursors else "，采集水位归零"))
    if not yes and not typer.confirm("确认执行？此操作不可撤销"):
        raise typer.Abort()

    async def _do() -> tuple[dict[str, int], dict[str, int]]:
        async with session_scope() as session:

            async def counts() -> dict[str, int]:
                out = {}
                for table in [*_DATA_TABLES, "source"]:
                    out[table] = (
                        await session.execute(text(f"select count(*) from {table}"))
                    ).scalar() or 0
                return out

            before = await counts()
            if session.bind.dialect.name == "postgresql":
                await session.execute(
                    text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
                )
            else:
                # SQLite 没有 TRUNCATE，逐表 DELETE
                for table in tables:
                    await session.execute(text(f"DELETE FROM {table}"))

            if not keep_cursors:
                await session.execute(
                    text(
                        "UPDATE source SET cursor_message_id=NULL, cursor_published_at=NULL, "
                        "backfill_cursor_id=NULL, backfill_done=false, total_collected=0, "
                        "total_backfilled=0, last_error=NULL, consecutive_failures=0, "
                        "next_fetch_at=NULL"
                    )
                )
                # extra 是 JSON 列，各方言的写法不同，交给 ORM 处理
                for source in await session.scalars(select(Source)):
                    source.extra = {}

            await session.commit()
            return before, await counts()

    before, after = _run(_do)
    _table(
        [[t, before[t], after[t]] for t in [*_DATA_TABLES, "source"]],
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
    from sqlalchemy import delete, func, select, update

    from funflix.base.db import session_scope
    from funflix.models import Tag, TagKind, media_tag
    from funflix.services.text.normalize import classify_tag

    if not yes and not typer.confirm("将重新归类全部标签并合并重复项，继续？"):
        raise typer.Abort()

    async def _do() -> dict[str, int]:
        stats = {"total": 0, "moved": 0, "merged": 0, "recounted": 0}
        async with session_scope() as session:
            tags = list(await session.scalars(select(Tag)))
            stats["total"] = len(tags)
            # 先建索引，避免每次都查库
            by_identity = {(t.kind.value, t.norm_key): t for t in tags}

            for tag in tags:
                new_kind = classify_tag(tag.name)
                if new_kind == tag.kind.value:
                    continue

                target = by_identity.get((new_kind, tag.norm_key))
                if target is None or target.id == tag.id:
                    tag.kind = TagKind(new_kind)
                    by_identity[(new_kind, tag.norm_key)] = tag
                    stats["moved"] += 1
                    continue

                # 目标维度下已有同名标签：把关联迁过去再删旧行。
                # 迁移前要剔掉两边都有的作品，否则会撞 (media_id, tag_id) 唯一键。
                dupes = select(media_tag.c.media_id).where(media_tag.c.tag_id == target.id)
                await session.execute(
                    update(media_tag)
                    .where(media_tag.c.tag_id == tag.id, media_tag.c.media_id.not_in(dupes))
                    .values(tag_id=target.id)
                )
                await session.execute(delete(media_tag).where(media_tag.c.tag_id == tag.id))
                await session.delete(tag)
                stats["merged"] += 1

            await session.flush()

            # 关联迁移后计数必然对不上，统一重算而不是增量维护
            counts = dict(
                (
                    await session.execute(
                        select(media_tag.c.tag_id, func.count()).group_by(media_tag.c.tag_id)
                    )
                ).all()
            )
            for tag in await session.scalars(select(Tag)):
                actual = counts.get(tag.id, 0)
                if tag.media_count != actual:
                    tag.media_count = actual
                    stats["recounted"] += 1

            await session.commit()
        return stats

    stats = _run(_do)
    _table(
        [
            ["标签总数", stats["total"]],
            ["改了维度", stats["moved"]],
            ["合并删除", stats["merged"]],
            ["修正计数", stats["recounted"]],
        ],
        ["项", "数量"],
    )
    _ok("标签重新归类完成")


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
    backfill_pages: Annotated[
        int | None, typer.Option(help="单轮往前回溯几页。补历史慢就调大它")
    ] = None,
    cursor: Annotated[str | None, typer.Option(help="回拨水位即可重采历史")] = None,
    title: Annotated[str | None, typer.Option(help="展示名")] = None,
) -> None:
    """修改采集源配置。

    追新（max_pages）和补历史（backfill_pages）是两个独立方向，互不影响。
    """
    from funflix.base.db import session_scope

    async def _do() -> None:
        async with session_scope() as session:
            source = await _require_source(session, source_id)
            if interval is not None:
                source.fetch_interval_seconds = interval
            if max_pages is not None:
                source.max_pages_per_fetch = max_pages
            if backfill_pages is not None:
                source.backfill_pages_per_fetch = backfill_pages
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
) -> None:
    """采集：把源里的新内容写成原始文本。"""
    from sqlalchemy import select

    from funflix.base.db import session_scope
    from funflix.models import Source
    from funflix.services.collect.runner import collect_source

    async def _do() -> list:
        async with session_scope() as session:
            if source_id is not None:
                targets = [await _require_source(session, source_id)]
            else:
                targets = list(await session.scalars(select(Source).where(Source.enabled)))
            if not targets:
                return []

            reports = []
            bar = _progress(targets, "采集", "源")
            for target in bar:
                if hasattr(bar, "set_postfix_str"):
                    bar.set_postfix_str(target.identifier[:20])
                reports.append((target.identifier, await collect_source(session, target)))
            await session.commit()
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
    limit: Annotated[int, typer.Option(help="本次最多解析多少条")] = 50,
    doc_id: Annotated[int | None, typer.Option(help="只解析指定文档")] = None,
    force: Annotated[bool, typer.Option(help="忽略缓存，强制重新抽取")] = False,
) -> None:
    """抽取：把原始文本解析成作品与资源。

    不指定抽取器时按来源类型自动选：表格源用 sheet，自由文本用 rule。
    用错抽取器不会报错，只会静默地大批归属失败，所以默认按源类型分开。
    """
    from sqlalchemy import or_, select

    from funflix.base.db import session_scope
    from funflix.models import RawDocument, utcnow
    from funflix.services.extract.registry import (
        default_extractor_for,
        get_extractor,
        supported_extractors,
    )
    from funflix.services.extract.runner import parse_document

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
        async with session_scope() as session:
            if doc_id is not None:
                doc = await session.get(RawDocument, doc_id)
                if doc is None:
                    _fail(f"文档 #{doc_id} 不存在")
                docs = [doc]
            else:
                now = utcnow()
                docs = list(
                    await session.scalars(
                        select(RawDocument)
                        .where(
                            RawDocument.parse_status == ParseStatus.PENDING,
                            or_(
                                RawDocument.next_parse_at.is_(None),
                                RawDocument.next_parse_at <= now,
                            ),
                        )
                        .order_by(RawDocument.id)
                        .limit(limit)
                    )
                )
            if not docs:
                return []

            reports = []
            bar = _progress(docs, "解析", "条")
            for doc in bar:
                impl = _extractor_for(doc)
                if hasattr(bar, "set_postfix_str"):
                    bar.set_postfix_str(impl.name)
                reports.append(await parse_document(session, doc, impl, force=force))
            await session.commit()
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
    limit: Annotated[int, typer.Option(help="本次最多校验多少条")] = 50,
    resource_id: Annotated[int | None, typer.Option(help="只校验指定资源")] = None,
    rate: Annotated[float, typer.Option(help="每个网盘每秒最多几次请求")] = 1.0,
    recheck_all: Annotated[
        bool, typer.Option("--recheck-all", help="忽略复查时间，重校验全部可校验资源")
    ] = False,
) -> None:
    """校验：探测网盘链接现在还能不能用。"""
    from sqlalchemy import or_, select

    from funflix.base.db import session_scope
    from funflix.base.enums import CHECKABLE_PROVIDERS
    from funflix.models import Resource, utcnow
    from funflix.services.verify.registry import assert_registry_matches_enum, get_probe
    from funflix.services.verify.runner import RateLimiter, check_resource

    assert_registry_matches_enum()
    limiter = RateLimiter(rate_per_second=rate)
    probes: dict[Any, Any] = {}

    async def _do() -> list:
        async with session_scope() as session:
            if resource_id is not None:
                target = await session.get(Resource, resource_id)
                if target is None:
                    _fail(f"资源 #{resource_id} 不存在")
                rows = [target]
            else:
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
                rows = list(
                    await session.scalars(
                        select(Resource)
                        .where(*conditions)
                        .order_by(Resource.next_check_at.nulls_first(), Resource.id)
                        .limit(limit)
                    )
                )
            if not rows:
                return []

            reports = []
            bar = _progress(rows, "校验", "条")
            for row in bar:
                probe = probes.setdefault(row.provider, get_probe(row.provider))
                if hasattr(bar, "set_postfix_str"):
                    bar.set_postfix_str(row.provider.value)
                reports.append(await check_resource(session, row, probe, limiter))
            await session.commit()
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
        "llm": "大模型抽取，质量最高，凭证走 nltsecret",
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
