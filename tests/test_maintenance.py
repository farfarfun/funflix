"""数据维护操作。

这两个操作**不可逆地改数据**，而在被提到服务层之前它们只存在于 Typer
命令体里，一条测试都没有 —— `db reset` 会 DELETE 全库，`db retag` 会
迁移关联并删标签行，两者都没有任何回归网。
"""

from __future__ import annotations

import pytest

from funflix.base.enums import MediaType, Provider, Quality, SourceType
from funflix.models import Media, RawDocument, Resource, Source, Tag, TagKind, media_tag, utcnow
from funflix.services.maintenance import (
    data_tables,
    recount_tags,
    reset_pipeline_data,
    retag_all,
)


def _source(n: int = 1) -> Source:
    return Source(
        source_type=SourceType.TELEGRAM,
        url=f"https://t.me/s/ch{n}",
        identifier=f"ch{n}",
        enabled=True,
        extra={},
    )


def _doc(n: int = 1) -> RawDocument:
    return RawDocument(
        content=f"名称：剧集{n}\n链接：https://pan.quark.cn/s/x{n:06d}",
        content_hash=f"{n:064d}",
        source_type=SourceType.MANUAL,
        collected_at=utcnow(),
        extra={},
    )


def _media(title: str = "剧集1") -> Media:
    return Media(title=title, norm_key=title, media_type=MediaType.MOVIE, year=2024, aliases=[])


def _resource(n: int = 1) -> Resource:
    now = utcnow()
    return Resource(
        provider=Provider.QUARK,
        share_id=f"s{n:06d}",
        url=f"https://pan.quark.cn/s/s{n:06d}",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
    )


class TestDataTables:
    def test_covers_every_table_except_config(self) -> None:
        """清单从 ORM 元数据推导，加了新表自动进来。

        曾经这里是写死的六元组，后来加的 tag / media_tag 没人记得补 ——
        `db reset` 之后 tag 行还在、media_count 还停在旧值，而 media 已经空了。
        """
        tables = set(data_tables())
        for expected in ["media", "resource", "raw_document", "tag", "media_tag", "media_resource"]:
            assert expected in tables, f"{expected} 不在清空清单里"
        assert "source" not in tables, "采集源是配置，不该被清空"

    def test_children_come_before_parents(self) -> None:
        """按外键依赖倒序，先删子表，否则 SQLite 开了外键约束会报错。"""
        tables = data_tables()
        assert tables.index("media_resource") < tables.index("media")
        assert tables.index("media_tag") < tables.index("tag")
        assert tables.index("resource") < tables.index("raw_document")

    def test_keep_documents_excludes_raw_document(self) -> None:
        assert "raw_document" not in data_tables(keep_documents=True)
        assert "resource" in data_tables(keep_documents=True)


@pytest.mark.asyncio
class TestResetPipelineData:
    async def test_clears_data_but_keeps_sources(self, session) -> None:
        session.add_all([_source(), _doc(), _media(), _resource()])
        await session.commit()

        report = await reset_pipeline_data(session)

        assert report.after["media"] == 0
        assert report.after["raw_document"] == 0
        assert report.after["resource"] == 0
        assert report.after["source"] == 1, "采集源配置必须保留"

    async def test_clears_tags_and_their_counters(self, session) -> None:
        """回归：tag 表曾被漏掉，reset 后留下 media_count 不为 0 的孤儿标签。

        再解析时这些标签按 norm_key 被复用，计数从错误的基数上继续累加，
        一次 reset 比一次离谱，而且全程没有任何报错。
        """
        media = _media()
        tag = Tag(kind=TagKind.GENRE, name="悬疑", norm_key="悬疑", media_count=1)
        session.add_all([media, tag])
        await session.flush()
        await session.execute(
            media_tag.insert().values(media_id=media.id, tag_id=tag.id, created_at=utcnow())
        )
        await session.commit()

        report = await reset_pipeline_data(session)

        assert report.after["tag"] == 0, "标签行没被清掉"
        assert report.after["media_tag"] == 0

    async def test_resets_watermark_by_default(self, session) -> None:
        """水位不归零的话，重建后采集器认为「都采过了」，一条也拉不回来。"""
        source = _source()
        source.cursor_message_id = "12345"
        source.total_collected = 99
        source.backfill_done = True
        source.extra = {"version": 7}
        session.add(source)
        await session.commit()

        await reset_pipeline_data(session)
        await session.refresh(source)

        assert source.cursor_message_id is None
        assert source.total_collected == 0
        assert source.backfill_done is False
        assert source.extra == {}, "采集器自定义水位也要清，否则一样会卡住"

    async def test_keep_cursors_preserves_watermark(self, session) -> None:
        source = _source()
        source.cursor_message_id = "12345"
        session.add(source)
        await session.commit()

        await reset_pipeline_data(session, keep_cursors=True)
        await session.refresh(source)

        assert source.cursor_message_id == "12345"

    async def test_keep_documents_implies_keep_cursors(self, session) -> None:
        """原始文本还在时归零水位没有意义 —— 重采回来的都会被 content_hash 挡掉。"""
        source = _source()
        source.cursor_message_id = "12345"
        session.add_all([source, _doc()])
        await session.commit()

        report = await reset_pipeline_data(session, keep_documents=True)
        await session.refresh(source)

        assert report.after["raw_document"] == 1
        assert source.cursor_message_id == "12345"
        assert report.cursors_reset is False


@pytest.mark.asyncio
class TestRetag:
    async def test_recount_fixes_drifted_counters(self, session) -> None:
        media = _media()
        tag = Tag(kind=TagKind.GENRE, name="悬疑", norm_key="悬疑", media_count=99)
        session.add_all([media, tag])
        await session.flush()
        await session.execute(
            media_tag.insert().values(media_id=media.id, tag_id=tag.id, created_at=utcnow())
        )
        await session.commit()

        assert await recount_tags(session) == 1
        await session.commit()
        await session.refresh(tag)
        assert tag.media_count == 1

    async def test_orphan_tag_counter_goes_to_zero(self, session) -> None:
        tag = Tag(kind=TagKind.GENRE, name="悬疑", norm_key="悬疑", media_count=5)
        session.add(tag)
        await session.commit()

        await recount_tags(session)
        await session.commit()
        await session.refresh(tag)
        assert tag.media_count == 0

    async def test_merges_duplicate_across_kinds(self, session) -> None:
        """同一个名字在新旧维度下各有一行时合并，关联迁走、旧行删掉。"""
        from funflix.services.text.normalize import classify_tag

        name = "悬疑"
        correct = classify_tag(name)
        wrong = TagKind.OTHER.value if correct != TagKind.OTHER.value else TagKind.GENRE.value

        media = _media()
        stale = Tag(kind=TagKind(wrong), name=name, norm_key=name, media_count=1)
        session.add_all([media, stale])
        await session.flush()
        await session.execute(
            media_tag.insert().values(media_id=media.id, tag_id=stale.id, created_at=utcnow())
        )
        await session.commit()

        report = await retag_all(session)

        assert report.total == 1
        assert report.moved == 1, f"{name} 应当从 {wrong} 挪到 {correct}"
        await session.refresh(stale)
        assert stale.kind.value == correct
        assert stale.media_count == 1


@pytest.mark.asyncio
class TestRequeueNowCheckable:
    """新增探针后，库里已有的那批链接必须能被放回队列。

    落库时不支持的 provider 会写成 unsupported + next_check_at=NULL，
    而领取条件要求 next_check_at 到期 —— 这些行永远不会被领取。
    于是加了 UC 探针之后，新的 UC 链接正常校验、老的永远停在 unsupported，
    两者混在一起很难注意到。
    """

    async def test_requeues_newly_supported_provider(self, session) -> None:
        from funflix.base.enums import CheckStatus
        from funflix.services.maintenance import requeue_now_checkable

        stale = _resource(1)
        stale.provider = Provider.UC
        stale.check_status = CheckStatus.UNSUPPORTED
        stale.next_check_at = None
        session.add(stale)
        await session.commit()

        assert await requeue_now_checkable(session) == 1
        await session.refresh(stale)
        assert stale.check_status is CheckStatus.UNCHECKED
        assert stale.next_check_at is not None

    async def test_leaves_still_unsupported_alone(self, session) -> None:
        """百度还没有探针，不能因为这条命令就被排进队列空转。"""
        from funflix.base.enums import CheckStatus
        from funflix.services.maintenance import requeue_now_checkable

        other = _resource(2)
        other.provider = Provider.BAIDU
        other.check_status = CheckStatus.UNSUPPORTED
        other.next_check_at = None
        session.add(other)
        await session.commit()

        assert await requeue_now_checkable(session) == 0
        await session.refresh(other)
        assert other.check_status is CheckStatus.UNSUPPORTED

    async def test_does_not_disturb_already_checked(self, session) -> None:
        """已有结论的资源不能被这条命令重置掉。"""
        from funflix.base.enums import CheckStatus
        from funflix.services.maintenance import requeue_now_checkable

        done = _resource(3)
        done.provider = Provider.UC
        done.check_status = CheckStatus.VALID
        session.add(done)
        await session.commit()

        assert await requeue_now_checkable(session) == 0
        await session.refresh(done)
        assert done.check_status is CheckStatus.VALID
