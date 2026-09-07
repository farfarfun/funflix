"""采集源的登记、管理与触发采集。

整个模块都要求登录（`CurrentUserDep`）。删掉一个源会连带丢掉它的水位游标，
重建后要么从头重采、要么漏掉中间的消息，这不该是匿名调用者能做到的事；
查询接口同样收进登录态之后，避免匿名枚举采集源列表。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from funflix.api.deps import CurrentUserDep, PageDep, SessionDep
from funflix.base.enums import ParseStatus, SourceType
from funflix.models import RawDocument, Resource, Source
from funflix.schemas.raw import Page
from funflix.schemas.source import (
    CollectReportOut,
    SourceCreate,
    SourceOut,
    SourceParseReportOut,
    SourceUpdate,
)
from funflix.services.collect.registry import detect_source, get_collector, supported_source_types
from funflix.services.collect.runner import collect_source
from funflix.worker.tasks import run_parse_once

router = APIRouter(prefix="/sources", tags=["sources"])

_ZERO_STATS = {"raw_total": 0, "raw_parsed": 0, "resource_total": 0}
#: 手动触发解析一批处理多少条——比后台 worker 的 limit=20 略宽，因为默认
#: 走本地 rule/sheet 抽取器（无外部调用），但仍要有界，不能让一次点击
#: 卡住整个同步请求。
_PARSE_TRIGGER_LIMIT = 50


async def _source_stats(
    session: SessionDep, source_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    """按源批量算「原始文本数 / 已解析数 / 解析出资源数」，避免逐源查询（N+1）。"""
    stats = {sid: dict(_ZERO_STATS) for sid in source_ids}
    if not source_ids:
        return stats

    raw_rows = await session.execute(
        select(RawDocument.source_id, RawDocument.parse_status, func.count())
        .where(RawDocument.source_id.in_(source_ids))
        .group_by(RawDocument.source_id, RawDocument.parse_status)
    )
    for source_id, parse_status, count in raw_rows.all():
        row = stats[source_id]
        row["raw_total"] += count
        if parse_status == ParseStatus.DONE:
            row["raw_parsed"] += count

    resource_rows = await session.execute(
        select(RawDocument.source_id, func.count(Resource.id))
        .select_from(Resource)
        .join(RawDocument, RawDocument.id == Resource.raw_document_id)
        .where(RawDocument.source_id.in_(source_ids))
        .group_by(RawDocument.source_id)
    )
    for source_id, count in resource_rows.all():
        stats[source_id]["resource_total"] = count

    return stats


def _with_stats(source: Source, stats: dict[str, int]) -> SourceOut:
    return SourceOut.model_validate(source).model_copy(update=stats)


@router.get("/supported", response_model=list[SourceType])
async def list_supported(_: CurrentUserDep) -> list[SourceType]:
    """当前实现了采集器的源类型。"""
    return supported_source_types()


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(payload: SourceCreate, session: SessionDep, _: CurrentUserDep) -> SourceOut:
    """登记一个采集源。

    同一个源（source_type + identifier）重复登记会返回 409 而不是建重复行 ——
    否则两条记录各持一份水位，会把同一批消息采两遍。
    """
    source_type = payload.source_type
    identifier = payload.identifier

    if source_type is None or identifier is None:
        detected = detect_source(payload.url)
        if detected is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"无法从 {payload.url!r} 识别采集源，"
                    f"请显式指定 source_type 与 identifier。"
                    f"当前支持：{[s.value for s in supported_source_types()]}"
                ),
            )
        source_type = source_type or detected[0]
        identifier = identifier or detected[1]

    if get_collector(source_type) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"暂不支持 {source_type.value} 类型的采集",
        )

    existing = await session.scalar(
        select(Source).where(Source.source_type == source_type, Source.identifier == identifier)
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"采集源已存在（id={existing.id}）：{source_type.value}/{identifier}",
        )

    source = Source(
        source_type=source_type,
        url=payload.url,
        identifier=identifier,
        title=payload.title,
        enabled=payload.enabled,
        fetch_interval_seconds=payload.fetch_interval_seconds,
        max_pages_per_fetch=payload.max_pages_per_fetch,
        cursor_message_id=payload.cursor_message_id,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    # 刚登记的源不可能有任何原始文本/资源，SourceOut 的统计字段默认值就是 0，
    # 跳过统计查询
    return SourceOut.model_validate(source)


@router.get("", response_model=Page[SourceOut])
async def list_sources(
    session: SessionDep,
    paging: PageDep,
    _: CurrentUserDep,
    enabled: bool | None = None,
    source_type: SourceType | None = None,
) -> Page[SourceOut]:
    conditions = []
    if enabled is not None:
        conditions.append(Source.enabled == enabled)
    if source_type is not None:
        conditions.append(Source.source_type == source_type)

    total = await session.scalar(select(func.count()).select_from(Source).where(*conditions))
    rows = list(
        await session.scalars(
            select(Source)
            .where(*conditions)
            .order_by(Source.id.desc())
            .offset(paging.offset)
            .limit(paging.size)
        )
    )
    stats = await _source_stats(session, [r.id for r in rows])
    return Page[SourceOut](
        items=[_with_stats(r, stats[r.id]) for r in rows],
        total=total or 0,
        page=paging.page,
        size=paging.size,
    )


async def _get_or_404(session: SessionDep, source_id: uuid.UUID) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="采集源不存在")
    return source


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: uuid.UUID, session: SessionDep, _: CurrentUserDep) -> SourceOut:
    source = await _get_or_404(session, source_id)
    stats = await _source_stats(session, [source.id])
    return _with_stats(source, stats[source.id])


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: uuid.UUID, payload: SourceUpdate, session: SessionDep, _: CurrentUserDep
) -> SourceOut:
    """修改采集源。把 `cursor_message_id` 回拨即可重采历史。"""
    source = await _get_or_404(session, source_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    await session.commit()
    await session.refresh(source)
    stats = await _source_stats(session, [source.id])
    return _with_stats(source, stats[source.id])


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_source(source_id: uuid.UUID, session: SessionDep, _: CurrentUserDep) -> None:
    """删除采集源。已采集的原始文本会保留（source_id 置空）。"""
    source = await _get_or_404(session, source_id)
    await session.delete(source)
    await session.commit()


@router.post("/{source_id}/collect", response_model=CollectReportOut)
async def trigger_collect(
    source_id: uuid.UUID, session: SessionDep, _: CurrentUserDep
) -> CollectReportOut:
    """立即采集一次（同步执行，便于接入时观察结果）。"""
    source = await _get_or_404(session, source_id)
    report = await collect_source(session, source)
    await session.commit()
    return CollectReportOut(
        source_id=report.source_id,
        ok=report.ok,
        fetched=report.fetched,
        created=report.created,
        duplicated=report.duplicated,
        skipped_empty=report.skipped_empty,
        pages_fetched=report.pages_fetched,
        truncated=report.truncated,
        cursor_before=report.cursor_before,
        cursor_after=report.cursor_after,
        error=report.error,
    )


@router.post("/{source_id}/parse", response_model=SourceParseReportOut)
async def trigger_parse(
    source_id: uuid.UUID, session: SessionDep, _: CurrentUserDep
) -> SourceParseReportOut:
    """立即解析该源一批待处理的原始文本（同步执行，单批，不排空整条队列）。

    与后台 worker 共用同一套租约领取机制（`claim_documents`），不会跟它
    重复处理同一条文档。
    """
    source = await _get_or_404(session, source_id)
    report = await run_parse_once(session, source_id=source.id, limit=_PARSE_TRIGGER_LIMIT)
    remaining = await session.scalar(
        select(func.count())
        .select_from(RawDocument)
        .where(
            RawDocument.source_id == source.id,
            RawDocument.parse_status == ParseStatus.PENDING,
        )
    )
    return SourceParseReportOut(
        source_id=source.id,
        claimed=report.claimed,
        succeeded=report.succeeded,
        failed=report.failed,
        reclaimed=report.reclaimed,
        abandoned=report.abandoned,
        remaining_pending=remaining or 0,
    )
