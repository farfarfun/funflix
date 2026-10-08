"""数据维护操作。

这两个操作**不可逆地改数据**，而在被提到服务层之前它们只存在于 Typer
命令体里，一条测试都没有 —— `db reset` 会 DELETE 全库，`db retag` 会
迁移关联并删标签行，两者都没有任何回归网。
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from funflix.base import dbconflict
from funflix.base.enums import CheckStatus, MediaType, ParseStatus, Provider, Quality, SourceType
from funflix.models import (
    LinkCheck,
    Media,
    RawDocument,
    Resource,
    Source,
    Tag,
    TagKind,
    Work,
    media_resource,
    media_tag,
    utcnow,
)
from funflix.services import maintenance
from funflix.services.maintenance import (
    PRUNE_MIN_AGE,
    cleanup_resources,
    data_tables,
    prune_empty_works,
    recount_tags,
    relink_checks,
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


#: 每行 media 都要有自己的 Work（`norm_key` 是唯一键，不能共用一个）。
_WORKS = itertools.count()


def _media(title: str = "剧集1") -> Media:
    """一行 media 连带它所属的作品。

    `media.work_id` 是 NOT NULL —— 一季必须属于某部作品（见 models/media.py），
    所以这里顺手把 Work 也造出来。这些测试关心的是资源/标签关联的维护，
    作品归属对它们来说只是过关条件。
    """
    work = Work(
        title=title,
        norm_key=f"{title}-{next(_WORKS)}",
        aliases=[],
        media_type=MediaType.MOVIE,
        year=2024,
    )
    return Media(
        title=title, norm_key=title, media_type=MediaType.MOVIE, year=2024, aliases=[], work=work
    )


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
        assert "user" not in tables, "登录账号是身份配置，不该被清空"

    def test_login_accounts_survive_a_rebuild(self) -> None:
        """账号在任何参数组合下都不能被清掉。

        密码哈希是单向的 —— 清了就只能重新 `funflix user create`，而生产库
        只有一个账号，清掉等于把运维区锁死。`purge_checks=True` 是「连最贵的
        校验历史都一起清」的最狠档位，账号在这一档也要留住。
        """
        for kwargs in ({}, {"keep_documents": True}, {"purge_checks": True}):
            assert "user" not in data_tables(**kwargs), f"user 不该出现在 {kwargs} 的清单里"

    def test_children_come_before_parents(self) -> None:
        """按外键依赖倒序，先删子表，否则 SQLite 开了外键约束会报错。"""
        tables = data_tables()
        assert tables.index("media_resource") < tables.index("media")
        assert tables.index("media_tag") < tables.index("tag")
        assert tables.index("resource") < tables.index("raw_document")

    def test_keep_documents_excludes_raw_document(self) -> None:
        assert "raw_document" not in data_tables(keep_documents=True)
        assert "resource" in data_tables(keep_documents=True)

    def test_link_check_excluded_by_default(self) -> None:
        """校验历史锚定在 (provider, share_id)，默认不该跟 resource 一起被清空。"""
        assert "link_check" not in data_tables()
        assert "resource" in data_tables()

    def test_purge_checks_includes_link_check(self) -> None:
        assert "link_check" in data_tables(purge_checks=True)


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

    async def test_keep_documents_requeues_already_parsed_documents(self, session) -> None:
        """回归：下游被清空后，`done`/`skipped` 状态的原始文本不重置就再也不会被重新解析。

        领取查询（`ix_raw_document_parse_queue`）只认 `parse_status == PENDING`，
        `reset_pipeline_data(keep_documents=True)` 从不碰 `raw_document` 表本身，
        之前解析完的文档会带着旧状态永久跳过下一轮 parse。
        """
        done_doc = _doc(1)
        done_doc.parse_status = ParseStatus.DONE
        done_doc.parse_attempts = 3
        done_doc.next_parse_at = utcnow()
        done_doc.last_parsed_at = utcnow()
        skipped_doc = _doc(2)
        skipped_doc.parse_status = ParseStatus.SKIPPED
        session.add_all([_source(), done_doc, skipped_doc])
        await session.commit()

        report = await reset_pipeline_data(session, keep_documents=True)
        await session.refresh(done_doc)
        await session.refresh(skipped_doc)

        assert report.documents_requeued == 2
        assert done_doc.parse_status is ParseStatus.PENDING
        assert done_doc.parse_attempts == 0
        assert done_doc.next_parse_at is None
        assert done_doc.last_parsed_at is None
        assert skipped_doc.parse_status is ParseStatus.PENDING

    async def test_preserves_link_check_by_default(self, session) -> None:
        """清空 resource 不该带走校验历史——它是全库成本最高的数据。"""
        resource = _resource()
        session.add(resource)
        await session.flush()
        session.add(
            LinkCheck(
                provider=resource.provider,
                share_id=resource.share_id,
                url=resource.url,
                checked_at=utcnow(),
                status=CheckStatus.VALID,
            )
        )
        await session.commit()

        report = await reset_pipeline_data(session)

        assert report.after["resource"] == 0
        assert report.after["link_check"] == 1, "校验历史不该被 reset 清空"
        assert report.checks_purged is False

    async def test_purge_checks_clears_link_check(self, session) -> None:
        resource = _resource()
        session.add(resource)
        await session.flush()
        session.add(
            LinkCheck(
                provider=resource.provider,
                share_id=resource.share_id,
                url=resource.url,
                checked_at=utcnow(),
                status=CheckStatus.VALID,
            )
        )
        await session.commit()

        report = await reset_pipeline_data(session, purge_checks=True)

        assert report.after["link_check"] == 0
        assert report.checks_purged is True


@pytest.mark.asyncio
async def test_cleanup_resources_reclassifies_merges_and_deletes(session) -> None:
    now = utcnow()
    media_a = _media("剧集A")
    media_b = _media("剧集B")
    target = Resource(
        provider=Provider.CTFILE,
        share_id="file/123",
        url="https://www.400gb.com/file/123",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
        seen_count=1,
    )
    duplicate_a = Resource(
        provider=Provider.OTHER,
        share_id="https://a.ctfile.com/file/123",
        url="https://a.ctfile.com/file/123",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
        seen_count=2,
    )
    duplicate_b = Resource(
        provider=Provider.OTHER,
        share_id="https://www.pipipan.com/file/123",
        url="https://www.pipipan.com/file/123",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
        seen_count=3,
    )
    unique_ctfile = Resource(
        provider=Provider.OTHER,
        share_id="https://www.400gb.com/fs/9-8",
        url="https://www.400gb.com/fs/9-8",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
    )
    blacklisted = Resource(
        provider=Provider.OTHER,
        share_id="https://t.me/channel",
        url="https://t.me/channel",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
    )
    legitimate_other = Resource(
        provider=Provider.OTHER,
        share_id="https://example.com/resource/1",
        url="https://example.com/resource/1",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add_all(
        [
            media_a,
            media_b,
            target,
            duplicate_a,
            duplicate_b,
            unique_ctfile,
            blacklisted,
            legitimate_other,
        ]
    )
    await session.flush()
    await session.execute(
        media_resource.insert(),
        [
            {"media_id": media_a.id, "resource_id": target.id, "created_at": now},
            {"media_id": media_a.id, "resource_id": duplicate_a.id, "created_at": now},
            {"media_id": media_b.id, "resource_id": duplicate_b.id, "created_at": now},
            {"media_id": media_a.id, "resource_id": blacklisted.id, "created_at": now},
        ],
    )
    await session.commit()

    report = await cleanup_resources(session)

    assert report.ctfile_found == 3
    assert report.ctfile_reclassified == 1
    assert report.duplicates_merged == 2
    assert report.blacklisted_deleted == 1
    kept = await session.scalar(
        select(Resource).where(
            Resource.provider == Provider.CTFILE, Resource.share_id == "file/123"
        )
    )
    assert kept is not None and kept.seen_count == 6
    assert await session.get(Resource, legitimate_other.id) is not None
    await session.refresh(media_a)
    await session.refresh(media_b)
    assert (media_a.resource_count, media_b.resource_count) == (1, 1)


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


@pytest.mark.asyncio
class TestRelinkChecks:
    """resource 被清空重建后，独立存储的校验历史要能按 (provider, share_id) 恢复状态。"""

    async def test_hydrates_matching_resource(self, session) -> None:
        history = LinkCheck(
            provider=Provider.QUARK,
            share_id="s000001",
            url="https://pan.quark.cn/s/s000001",
            checked_at=utcnow(),
            status=CheckStatus.VALID,
            detail="ok",
        )
        session.add(history)
        rebuilt = _resource(1)
        session.add(rebuilt)
        await session.commit()
        assert rebuilt.check_status is CheckStatus.UNCHECKED

        report = await relink_checks(session)

        assert report.hydrated == 1
        await session.refresh(rebuilt)
        assert rebuilt.check_status is CheckStatus.VALID
        assert rebuilt.last_checked_at == history.checked_at
        assert rebuilt.next_check_at is not None, "恢复后仍要能重新进入复查队列"

    async def test_ignores_history_without_matching_resource(self, session) -> None:
        session.add(
            LinkCheck(
                provider=Provider.QUARK,
                share_id="s999999",
                url="https://pan.quark.cn/s/s999999",
                checked_at=utcnow(),
                status=CheckStatus.VALID,
            )
        )
        await session.commit()

        report = await relink_checks(session)

        assert report.hydrated == 0

    async def test_does_not_overwrite_already_checked_resource(self, session) -> None:
        """resource 已经有真实结论（不是重建后的默认 UNCHECKED）时不能被历史覆盖。"""
        session.add(
            LinkCheck(
                provider=Provider.QUARK,
                share_id="s000002",
                url="https://pan.quark.cn/s/s000002",
                checked_at=utcnow(),
                status=CheckStatus.INVALID,
            )
        )
        already_checked = _resource(2)
        already_checked.check_status = CheckStatus.VALID
        session.add(already_checked)
        await session.commit()

        report = await relink_checks(session)

        assert report.hydrated == 0
        await session.refresh(already_checked)
        assert already_checked.check_status is CheckStatus.VALID

    async def test_latest_history_wins(self, session) -> None:
        """一个链接有多条历史时只认最新那条，否则会把早已失效的链接恢复成有效。"""
        base = utcnow()
        for offset, status in ((0, CheckStatus.VALID), (1, CheckStatus.INVALID)):
            session.add(
                LinkCheck(
                    provider=Provider.QUARK,
                    share_id="s000004",
                    url="https://pan.quark.cn/s/s000004",
                    checked_at=base + timedelta(hours=offset),
                    status=status,
                )
            )
            await session.flush()  # 逐条 flush 才能保证 id 单调递增
        rebuilt = _resource(4)
        session.add(rebuilt)
        await session.commit()

        report = await relink_checks(session)

        assert report.hydrated == 1
        await session.refresh(rebuilt)
        assert rebuilt.check_status is CheckStatus.INVALID

    async def test_restores_every_link_across_batch_boundaries(self, session, monkeypatch) -> None:
        """攒批 executemany 的边界：末尾那个不满一批的残批不能被漏掉。

        84 万次往返改成攒批之后，「最后一批没发出去」是这段代码最容易出的
        静默错误 —— 它不报错，只是有一部分链接的校验结论没恢复，
        于是全量重建之后那些资源会被重新探测一遍。
        """
        monkeypatch.setattr(maintenance, "_RELINK_BATCH", 2)
        for n in range(10, 15):  # 5 条 = 两个满批 + 一个残批
            session.add(
                LinkCheck(
                    provider=Provider.QUARK,
                    share_id=f"s{n:06d}",
                    url=f"https://pan.quark.cn/s/s{n:06d}",
                    checked_at=utcnow(),
                    status=CheckStatus.VALID,
                )
            )
            session.add(_resource(n))
        await session.commit()

        report = await relink_checks(session)

        assert report.hydrated == 5
        remaining = await session.scalars(
            select(Resource).where(Resource.check_status == CheckStatus.UNCHECKED)
        )
        assert list(remaining) == []

    async def test_a_batch_that_keeps_deadlocking_is_skipped(self, session, monkeypatch) -> None:
        """一批撞车重试耗尽，只放弃这批、继续往下走，**不能把整个命令带崩**。

        这个函数做的是「省一遍重探」的优化：放弃的行留在 UNCHECKED，verify
        会照常去探，结论一样。而让异常冒出去的代价是整个 verify job 在
        `Relink` 这一步退出 1 —— 实测线上就这么丢了一整轮，后面的
        `funflix verify` 一条都没跑（run 37626929998）。
        """
        monkeypatch.setattr(maintenance, "_RELINK_BATCH", 2)
        monkeypatch.setattr(dbconflict, "WRITE_CONFLICT_BACKOFF", 0.0)
        for n in range(20, 24):  # 两个满批
            session.add(
                LinkCheck(
                    provider=Provider.QUARK,
                    share_id=f"s{n:06d}",
                    url=f"https://pan.quark.cn/s/s{n:06d}",
                    checked_at=utcnow(),
                    status=CheckStatus.VALID,
                )
            )
            session.add(_resource(n))
        await session.commit()

        real_execute = session.execute
        seen = 0

        async def flaky(stmt, params=None, *args, **kwargs):
            """第一批（只有第一批）每次都撞死锁，第二批正常落。"""
            nonlocal seen
            if isinstance(params, list):
                seen += 1
                if seen <= dbconflict.WRITE_CONFLICT_ATTEMPTS:
                    orig = Exception("deadlock detected")
                    orig.sqlstate = "40P01"  # type: ignore[attr-defined]
                    raise DBAPIError("UPDATE resource ...", {}, orig)
            return await real_execute(stmt, params, *args, **kwargs)

        monkeypatch.setattr(session, "execute", flaky)
        report = await relink_checks(session)
        monkeypatch.undo()

        assert report.conflicted == 2, "放弃的那一批要如实报出来"
        assert report.hydrated == 2, "另一批照常恢复"
        remaining = await session.scalars(
            select(Resource.share_id).where(Resource.check_status == CheckStatus.UNCHECKED)
        )
        assert len(list(remaining)) == 2, "放弃的行留在 UNCHECKED，交给 verify 正常探测"

    async def test_a_finished_batch_is_committed_before_the_next_one_runs(
        self, session, monkeypatch
    ) -> None:
        """每批落完就提交 —— 提交点在哪决定了行锁要持有多久。

        原先 424 个批次全挤在同一个外层事务里、只用 SAVEPOINT 分隔，`commit`
        等到整个函数末尾才发：第一批拿到的 2000 把行锁一路持到二十分钟后，
        锁越攒越多，而 CI 里四个 parse 分片正在写同一批 `resource`。
        run 37744553498 的 Relink 实测 30 个批次撞车重试耗尽 —— 重试次数和
        退避时长都治不了，锁窗口才是主因。

        提交点的位置不好直接断言，这里用「第二批抛个非冲突异常」把它照出来：
        没有按批提交的话，第一批会跟着第二批的异常一起回滚。
        """
        monkeypatch.setattr(maintenance, "_RELINK_BATCH", 2)
        for n in range(30, 34):  # 两个满批
            session.add(
                LinkCheck(
                    provider=Provider.QUARK,
                    share_id=f"s{n:06d}",
                    url=f"https://pan.quark.cn/s/s{n:06d}",
                    checked_at=utcnow(),
                    status=CheckStatus.VALID,
                )
            )
            session.add(_resource(n))
        await session.commit()

        real_execute = session.execute
        seen = 0

        async def boom(stmt, params=None, *args, **kwargs):
            """第一批正常落，第二批抛个**不是**并发冲突的异常。"""
            nonlocal seen
            if isinstance(params, list):
                seen += 1
                if seen == 2:
                    raise RuntimeError("第二批炸了")
            return await real_execute(stmt, params, *args, **kwargs)

        monkeypatch.setattr(session, "execute", boom)
        with pytest.raises(RuntimeError):
            await relink_checks(session)
        monkeypatch.undo()
        await session.rollback()

        remaining = list(
            await session.scalars(
                select(Resource.share_id).where(Resource.check_status == CheckStatus.UNCHECKED)
            )
        )
        assert len(remaining) == 2, "第一批已经提交，不该被第二批的异常带走"


class TestPruneEmptyWorks:
    """空壳作品（没有任何 media 指向）要删掉。

    这些行是 rehome / merge 的残留。搜索默认不过滤它们（见
    `services/search.py::_apply_filters`：只有 `valid_only` 那条路才要求有
    校验通过的资源），所以空壳会直接出现在列表页里、点进去什么都没有 ——
    生产库实测 36,223 行、占作品总数 17.9%。
    """

    def _work(self, title: str, *, age=PRUNE_MIN_AGE * 2) -> Work:
        """一个光秃秃的作品，没有任何 media。

        `created_at` 要显式写老：`prune_empty_works` 只删静置够久的行。
        """
        return Work(
            title=title,
            norm_key=title,
            aliases=[],
            media_type=MediaType.MOVIE,
            year=2024,
            created_at=utcnow() - age,
        )

    @pytest.mark.asyncio
    async def test_deletes_a_work_with_no_media(self, session) -> None:
        session.add(self._work("空壳"))
        await session.commit()

        report = await prune_empty_works(session)
        assert report.deleted == 1
        assert await session.scalar(select(func.count()).select_from(Work)) == 0

    @pytest.mark.asyncio
    async def test_keeps_a_work_that_still_has_a_season(self, session) -> None:
        """有 media 指向就不能删 —— `work_id` 是 CASCADE，删了会连带删掉季。"""
        media = _media("流浪地球")
        session.add_all([media, self._work("空壳")])
        await session.commit()

        report = await prune_empty_works(session)
        assert report.deleted == 1
        survivors = set(await session.scalars(select(Work.title)))
        assert survivors == {"流浪地球"}

    @pytest.mark.asyncio
    async def test_does_not_trust_a_stale_season_count(self, session) -> None:
        """判定只看有没有 media 指向，不看冗余计数。

        `season_count` 由 `services/counters.py` 事后重算，本身会过期：生产库
        里 `season_count == 0` 是 33,633 行，真的没有 media 指向的是 36,223
        行 —— 两边都会错，而删行是不可逆的。
        """
        # 有季、但计数还没刷到（看起来像空壳）
        live = _media("计数没刷到")
        live.work.season_count = 0
        live.work.resource_count = 0
        # 真空壳、但计数停留在旧值（看起来像有内容）
        shell = self._work("计数是旧的")
        shell.season_count = 3
        shell.resource_count = 7
        session.add_all([live, shell])
        await session.commit()

        report = await prune_empty_works(session)
        assert report.deleted == 1
        survivors = set(await session.scalars(select(Work.title)))
        assert survivors == {"计数没刷到"}, "该按关联判，不该按计数判"

    @pytest.mark.asyncio
    async def test_a_freshly_created_work_is_left_alone(self, session) -> None:
        """刚建出来的不碰。

        建作品和挂 media 目前在同一个事务里，所以理论上不存在「已提交的空
        作品」这种中间态。但删行不可逆，万一将来有哪条路径把两件事拆开提交，
        这个静置窗口就是唯一的兜底。
        """
        session.add(self._work("刚建的", age=timedelta(0)))
        await session.commit()

        report = await prune_empty_works(session)
        assert report.deleted == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 1

    @pytest.mark.asyncio
    async def test_limit_caps_the_round_and_reports_the_rest(self, session, monkeypatch) -> None:
        """分轮删：首轮三万多行，一次删完的大事务会把 CI 的 job 预算占满。"""
        monkeypatch.setattr(maintenance, "PRUNE_CHUNK", 2)
        session.add_all([self._work(f"空壳{i}") for i in range(5)])
        await session.commit()

        report = await prune_empty_works(session, limit=3)
        assert (report.deleted, report.remaining) == (3, 2)
        assert await session.scalar(select(func.count()).select_from(Work)) == 2

        # 下一轮把剩下的收干净
        again = await prune_empty_works(session, limit=3)
        assert (again.deleted, again.remaining) == (2, 0)
        assert await session.scalar(select(func.count()).select_from(Work)) == 0
