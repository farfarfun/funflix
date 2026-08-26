"""采集源的登记、管理与触发采集。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from funflix.api.deps import SessionDep
from funflix.base.enums import SourceType
from funflix.models import Source
from funflix.schemas.raw import Page
from funflix.schemas.source import CollectReportOut, SourceCreate, SourceOut, SourceUpdate
from funflix.services.collect.registry import detect_source, get_collector, supported_source_types
from funflix.services.collect.runner import collect_source

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("/supported", response_model=list[SourceType])
async def list_supported() -> list[SourceType]:
    """当前实现了采集器的源类型。"""
    return supported_source_types()


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(payload: SourceCreate, session: SessionDep) -> SourceOut:
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
    return SourceOut.model_validate(source)


@router.get("", response_model=Page[SourceOut])
async def list_sources(
    session: SessionDep,
    enabled: bool | None = None,
    source_type: SourceType | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> Page[SourceOut]:
    conditions = []
    if enabled is not None:
        conditions.append(Source.enabled == enabled)
    if source_type is not None:
        conditions.append(Source.source_type == source_type)

    total = await session.scalar(select(func.count()).select_from(Source).where(*conditions))
    rows = await session.scalars(
        select(Source)
        .where(*conditions)
        .order_by(Source.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    return Page[SourceOut](
        items=[SourceOut.model_validate(r) for r in rows],
        total=total or 0,
        page=page,
        size=size,
    )


async def _get_or_404(session: SessionDep, source_id: int) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="采集源不存在")
    return source


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: int, session: SessionDep) -> SourceOut:
    return SourceOut.model_validate(await _get_or_404(session, source_id))


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(source_id: int, payload: SourceUpdate, session: SessionDep) -> SourceOut:
    """修改采集源。把 `cursor_message_id` 回拨即可重采历史。"""
    source = await _get_or_404(session, source_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    await session.commit()
    await session.refresh(source)
    return SourceOut.model_validate(source)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: int, session: SessionDep) -> None:
    """删除采集源。已采集的原始文本会保留（source_id 置空）。"""
    source = await _get_or_404(session, source_id)
    await session.delete(source)
    await session.commit()


@router.post("/{source_id}/collect", response_model=CollectReportOut)
async def trigger_collect(source_id: int, session: SessionDep) -> CollectReportOut:
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
