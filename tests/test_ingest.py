from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import StatementError

from funflix.base.enums import ParseStatus, SourceType
from funflix.models import RawDocument
from funflix.schemas.raw import RawDocumentCreate
from funflix.services.ingest import content_hash, ingest_document, ingest_many, normalize_for_hash


class TestNormalizeForHash:
    def test_strips_trailing_whitespace_and_blank_lines(self) -> None:
        assert normalize_for_hash("影视剧A  \n\n\n链接x  \n") == "影视剧A\n链接x"

    def test_normalizes_line_endings(self) -> None:
        assert normalize_for_hash("a\r\nb\rc") == "a\nb\nc"

    def test_preserves_line_structure(self) -> None:
        """换行是「一条文本含多部作品」的边界信号，不能压成空格。"""
        assert normalize_for_hash("剧A\n剧B") != normalize_for_hash("剧A 剧B")

    def test_hash_is_stable_across_cosmetic_noise(self) -> None:
        assert content_hash("剧A\n链接x") == content_hash("  剧A  \n\n链接x\n\n")


@pytest.mark.asyncio
class TestIngestDocument:
    async def test_creates_document_with_pending_status(self, session) -> None:
        payload = RawDocumentCreate(
            content="剧名A\nhttps://example.com/s/abc",
            source_type=SourceType.TELEGRAM,
            source_name="某频道",
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        outcome = await ingest_document(session, payload)
        await session.commit()

        assert outcome.duplicated is False
        doc = outcome.document
        assert doc.id is not None
        assert doc.parse_status is ParseStatus.PENDING
        assert doc.parse_attempts == 0
        # 立刻可被 worker 领取
        assert doc.next_parse_at is not None

    async def test_duplicate_content_returns_existing(self, session) -> None:
        first = await ingest_document(session, RawDocumentCreate(content="剧名A\n链接x"))
        await session.commit()

        # 排版有差异但语义相同
        second = await ingest_document(session, RawDocumentCreate(content="  剧名A  \n\n链接x\n"))
        await session.commit()

        assert second.duplicated is True
        assert second.document.id == first.document.id

    async def test_different_content_creates_separate_rows(self, session) -> None:
        a = await ingest_document(session, RawDocumentCreate(content="剧名A"))
        b = await ingest_document(session, RawDocumentCreate(content="剧名B"))
        await session.commit()
        assert a.document.id != b.document.id

    async def test_timestamps_are_timezone_aware(self, session) -> None:
        """SQLite 读回来默认是 naive 的，UTCDateTime 必须补齐 tzinfo。"""
        outcome = await ingest_document(session, RawDocumentCreate(content="剧名A"))
        await session.commit()
        doc_id = outcome.document.id  # 必须在 expire 前取，否则触发异步外的懒加载
        session.expire_all()

        doc = await session.get(RawDocument, doc_id)
        assert doc.collected_at.tzinfo is not None
        assert doc.created_at.tzinfo is not None

    async def test_rejects_naive_datetime(self, session) -> None:
        """写入 naive datetime 会让 SQLite 与 PG 行为分叉，必须在边界拦住。"""
        payload = RawDocumentCreate(content="剧名A")
        payload.published_at = datetime(2026, 8, 1)  # 无 tzinfo
        # SQLAlchemy 会把类型层抛出的 ValueError 包成 StatementError
        with pytest.raises(StatementError, match="naive datetime"):
            await ingest_document(session, payload)


@pytest.mark.asyncio
class TestIngestMany:
    async def test_deduplicates_within_batch(self, session) -> None:
        payloads = [
            RawDocumentCreate(content="剧名A"),
            RawDocumentCreate(content="剧名B"),
            RawDocumentCreate(content="剧名A"),  # 同批次内重复
        ]
        outcomes = await ingest_many(session, payloads)
        await session.commit()

        assert [o.duplicated for o in outcomes] == [False, False, True]
        assert outcomes[0].document.id == outcomes[2].document.id
