"""原始文本的摄入与查询接口。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from funflix.api.deps import PageDep, SessionDep, SettingsDep
from funflix.base.enums import ParseStatus, SourceType
from funflix.models import RawDocument
from funflix.schemas.raw import (
    BatchIngestResult,
    IngestResult,
    Page,
    RawDocumentBatchCreate,
    RawDocumentCreate,
    RawDocumentOut,
    RawDocumentSummary,
)
from funflix.services.ingest import IngestOutcome, ingest_document, ingest_many

router = APIRouter(prefix="/raw", tags=["raw"])


def _to_result(outcome: IngestOutcome) -> IngestResult:
    return IngestResult(
        id=outcome.document.id,
        content_hash=outcome.document.content_hash,
        duplicated=outcome.duplicated,
        parse_status=outcome.document.parse_status,
    )


def _guard_length(content: str, settings: SettingsDep) -> None:
    if len(content) > settings.ingest_max_content_length:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"原始文本长度 {len(content)} 超过上限 "
                f"{settings.ingest_max_content_length}，请拆分后提交"
            ),
        )


@router.post("", response_model=IngestResult, status_code=status.HTTP_201_CREATED)
async def create_raw_document(
    payload: RawDocumentCreate,
    session: SessionDep,
    settings: SettingsDep,
) -> IngestResult:
    """提交一条原始分享文本。

    命中 content_hash 时返回已有记录且 `duplicated=true`，不会重复触发后续解析。
    """
    _guard_length(payload.content, settings)
    outcome = await ingest_document(session, payload)
    await session.commit()
    return _to_result(outcome)


@router.post("/bulk", response_model=BatchIngestResult, status_code=status.HTTP_201_CREATED)
async def create_raw_documents(
    payload: RawDocumentBatchCreate,
    session: SessionDep,
    settings: SettingsDep,
) -> BatchIngestResult:
    """批量提交。整批在一个事务里，要么全成要么全滚。"""
    if len(payload.items) > settings.ingest_max_batch:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"单批最多 {settings.ingest_max_batch} 条，收到 {len(payload.items)} 条",
        )
    for item in payload.items:
        _guard_length(item.content, settings)

    outcomes = await ingest_many(session, payload.items)
    await session.commit()

    results = [_to_result(o) for o in outcomes]
    duplicated = sum(1 for r in results if r.duplicated)
    return BatchIngestResult(
        total=len(results),
        created=len(results) - duplicated,
        duplicated=duplicated,
        items=results,
    )


@router.get("", response_model=Page[RawDocumentSummary])
async def list_raw_documents(
    session: SessionDep,
    paging: PageDep,
    parse_status: ParseStatus | None = None,
    source_type: SourceType | None = None,
    source_name: str | None = None,
) -> Page[RawDocumentSummary]:
    """按状态 / 来源翻页查看原始文本，不返回全文。"""
    conditions = []
    if parse_status is not None:
        conditions.append(RawDocument.parse_status == parse_status)
    if source_type is not None:
        conditions.append(RawDocument.source_type == source_type)
    if source_name is not None:
        conditions.append(RawDocument.source_name == source_name)

    total = await session.scalar(select(func.count()).select_from(RawDocument).where(*conditions))
    rows = await session.scalars(
        select(RawDocument)
        .where(*conditions)
        .order_by(RawDocument.id.desc())
        .offset(paging.offset)
        .limit(paging.size)
    )
    return Page[RawDocumentSummary](
        items=[RawDocumentSummary.model_validate(r) for r in rows],
        total=total or 0,
        page=paging.page,
        size=paging.size,
    )


@router.get("/{doc_id}", response_model=RawDocumentOut)
async def get_raw_document(doc_id: uuid.UUID, session: SessionDep) -> RawDocumentOut:
    doc = await session.get(RawDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="原始文本不存在")
    return RawDocumentOut.model_validate(doc)
