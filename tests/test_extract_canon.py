"""入库路径查 `title_canon` —— 防止新数据把并好的作品重新拆开。

归一的四个阶段是**一次性补救**，这一层才是长期保障：阶段 1~4 跑完之后，
频道里每天继续涌进来的新分享如果还按老规则各自建行，几天就把并好的
结果重新摊成一地。所以这里的断言针对的都是「回退」这一类失败：

- 同一部剧的不同季写法 → 一个 Work、两行 media（不是两个 Work）
- 裁决说「这个脏键属于大主宰」→ 真的挂到大主宰，不另立门户
- 裁决说「这不是作品」→ 链接降级成未归属资源，**不丢**
- 没见过的键 → 自动留一行 `pending`，下一轮 `canon resolve` 能捞到
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from funflix.base.enums import MediaType, ParseStatus, SourceType
from funflix.models import Media, RawDocument, Resource, Work, media_resource, utcnow
from funflix.models.canon import CanonState, TitleCanon
from funflix.models.media import NO_SEASON, UNKNOWN_YEAR
from funflix.services.canon.lookup import resolve_target
from funflix.services.extract.rule import RuleExtractor
from funflix.services.extract.runner import parse_batch, parse_document


def _doc(n: int, title: str) -> RawDocument:
    return RawDocument(
        content=f"名称：{title}\n链接：https://pan.quark.cn/s/fake{n:06d}",
        content_hash=f"hash{n:060d}",
        source_type=SourceType.MANUAL,
        collected_at=utcnow(),
        parse_status=ParseStatus.PENDING,
        extra={},
    )


def _canon(norm_key: str, **kw) -> TitleCanon:
    kw.setdefault("status", CanonState.DECIDED)
    kw.setdefault("media_type", MediaType.UNKNOWN)
    kw.setdefault("year", UNKNOWN_YEAR)
    kw.setdefault("is_junk", False)
    return TitleCanon(norm_key=norm_key, **kw)


class TestResolveTarget:
    """纯函数层。决策错一次就是一个虚假作品，所以单独测。"""

    def test_no_canon_falls_back_to_rules_and_asks_for_a_pending_row(self) -> None:
        target = resolve_target(
            title="大主宰 第二季", media_type=MediaType.ANIME, year=2023, canon=None
        )
        assert target.work_norm_key == "大主宰"
        assert target.season == 2
        assert target.needs_pending_row is True
        assert target.is_junk is False

    def test_pending_canon_uses_rules_but_does_not_ask_for_another_row(self) -> None:
        """已经有 pending 行了，再插一行会撞主键、把整个 SAVEPOINT 带崩。"""
        target = resolve_target(
            title="大主宰",
            media_type=MediaType.UNKNOWN,
            year=None,
            canon=_canon("大主宰", status=CanonState.PENDING),
        )
        assert target.needs_pending_row is False
        assert target.work_norm_key == "大主宰"

    def test_decided_canon_overrides_the_rule_derived_work(self) -> None:
        """这是整张表存在的理由：规则认不出的脏键靠裁决归位。"""
        target = resolve_target(
            title="大主宰iq",
            media_type=MediaType.UNKNOWN,
            year=None,
            canon=_canon(
                "大主宰iq",
                work_norm_key="大主宰",
                work_title="大主宰",
                season=2,
                media_type=MediaType.ANIME,
                year=2023,
            ),
        )
        assert target.work_norm_key == "大主宰"
        assert target.season == 2
        assert target.media_type is MediaType.ANIME
        assert target.year == 2023

    def test_null_season_in_canon_keeps_the_per_row_rule_verdict(self) -> None:
        """`season=None` 是「这个键没锁定季」，不是「第 0 季」。

        把它当 0 处理会把一部剧的所有季压成一行 —— 正是这次改造要消掉的毛病。
        """
        canon = _canon("大主宰", work_norm_key="大主宰", work_title="大主宰", season=None)
        first = resolve_target(
            title="大主宰 第一季", media_type=MediaType.UNKNOWN, year=None, canon=canon
        )
        second = resolve_target(
            title="大主宰 第二季", media_type=MediaType.UNKNOWN, year=None, canon=canon
        )
        assert (first.season, second.season) == (1, 2)

    def test_canon_does_not_downgrade_a_known_type(self) -> None:
        """裁决里是 unknown 时用本条判出的值补，反过来不覆盖。"""
        target = resolve_target(
            title="大主宰",
            media_type=MediaType.ANIME,
            year=2023,
            canon=_canon("大主宰", work_norm_key="大主宰", work_title="大主宰"),
        )
        assert target.media_type is MediaType.ANIME
        assert target.year == 2023

    def test_junk_decision_reports_junk(self) -> None:
        target = resolve_target(
            title="大主宰 84",
            media_type=MediaType.UNKNOWN,
            year=None,
            canon=_canon("大主宰84", is_junk=True),
        )
        assert target.is_junk is True

    def test_a_type_only_decision_takes_the_type_and_leaves_identity_to_the_rules(self) -> None:
        """分类裁决（`resolver.py` 阶段 2）落下来的行走的就是这条路。

        它只有 `media_type`，`work_title` / `work_norm_key` 都是 NULL —— 这是
        刻意的，孤立的键没有可并的对象，给模型一个写标题的字段只会凭空制造
        误并的机会。所以身份必须从这一条分享自己的标题来，类型从裁决来。

        而且**不能**回落到 `_fallback`：那会把 `needs_pending_row` 置真，可这
        个键在库里已经有行了，`runner.py` 再插一行就撞主键、把整个 SAVEPOINT
        带崩（见那边的注释）。
        """
        target = resolve_target(
            title="沉默不语的顾小姐 更新至30集",
            media_type=MediaType.UNKNOWN,
            year=None,
            canon=_canon(
                "沉默不语的顾小姐",
                work_norm_key=None,
                work_title=None,
                season=None,
                media_type=MediaType.TV,
            ),
        )
        assert target.media_type is MediaType.TV
        assert target.work_norm_key == "沉默不语的顾小姐"
        assert target.work_title == "沉默不语的顾小姐 更新至30集"
        assert target.needs_pending_row is False

    def test_broken_decision_falls_back_instead_of_making_an_empty_key_work(self) -> None:
        """裁决的作品名洗完是空的 —— 别建一个空键 Work，那是吸收脏数据的黑洞。"""
        target = resolve_target(
            title="大主宰",
            media_type=MediaType.UNKNOWN,
            year=None,
            canon=_canon("大主宰", work_norm_key=None, work_title="   "),
        )
        assert target.work_norm_key == "大主宰"
        assert target.needs_pending_row is True


class TestUpsertRoutesThroughCanon:
    @pytest.mark.asyncio
    async def test_two_seasons_land_under_one_work(self, session) -> None:
        """最核心的一条：季不再各自成为一部作品。"""
        docs = [_doc(1, "大主宰 第一季"), _doc(2, "大主宰 第二季")]
        session.add_all(docs)
        await session.commit()

        reports = await parse_batch(session, docs, RuleExtractor())
        await session.commit()
        assert all(r.ok for r in reports)

        works = list(await session.scalars(select(Work)))
        assert len(works) == 1
        assert works[0].norm_key == "大主宰"

        seasons = sorted(await session.scalars(select(Media.season)))
        assert seasons == [1, 2]
        assert {m.work_id for m in await session.scalars(select(Media))} == {works[0].id}

    @pytest.mark.asyncio
    async def test_a_new_key_leaves_a_pending_row_for_resolve(self, session) -> None:
        doc = _doc(1, "大主宰 第二季")
        session.add(doc)
        await session.commit()

        await parse_document(session, doc, RuleExtractor())
        await session.commit()

        rows = list(await session.scalars(select(TitleCanon)))
        assert len(rows) == 1
        assert rows[0].status == CanonState.PENDING
        assert rows[0].work_norm_key == "大主宰"
        # 这一列的语义是「**这个键**锁定了哪一季」。写进本条分享的季号就等于
        # 拿一条分享的判断锁住整个键。
        assert rows[0].season is None

    @pytest.mark.asyncio
    async def test_a_decided_key_is_not_overwritten_with_a_pending_row(self, session) -> None:
        session.add(_canon("大主宰", work_norm_key="大主宰", work_title="大主宰"))
        doc = _doc(1, "大主宰")
        session.add(doc)
        await session.commit()

        await parse_document(session, doc, RuleExtractor())
        await session.commit()

        rows = list(await session.scalars(select(TitleCanon)))
        assert len(rows) == 1
        assert rows[0].status == CanonState.DECIDED

    @pytest.mark.asyncio
    async def test_a_decided_key_rehomes_a_title_the_rules_cannot_parse(self, session) -> None:
        """`大主宰iq` 规则认不出，裁决把它挂到大主宰的第 2 季上。"""
        session.add(
            _canon(
                "大主宰iq",
                work_norm_key="大主宰",
                work_title="大主宰",
                season=2,
                media_type=MediaType.ANIME,
            )
        )
        docs = [_doc(1, "大主宰 第二季"), _doc(2, "大主宰IQ")]
        session.add_all(docs)
        await session.commit()

        await parse_batch(session, docs, RuleExtractor())
        await session.commit()

        assert await session.scalar(select(func.count()).select_from(Work)) == 1
        media_rows = list(await session.scalars(select(Media)))
        assert len(media_rows) == 1, "两条分享该落到同一季的同一行上"
        links = list(await session.scalars(select(media_resource.c.resource_id)))
        assert len(links) == 2

    @pytest.mark.asyncio
    async def test_a_junk_decision_keeps_the_link_as_an_unattributed_resource(
        self, session
    ) -> None:
        """链接是真实采集成本 —— 判成垃圾的是**标题**，不是链接。"""
        session.add(_canon("大主宰84", is_junk=True))
        doc = _doc(1, "大主宰 84")
        session.add(doc)
        await session.commit()

        report = await parse_document(session, doc, RuleExtractor())
        await session.commit()

        assert report.ok
        assert report.unattributed_links == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0
        assert await session.scalar(select(func.count()).select_from(Resource)) == 1
        assert await session.scalar(select(func.count()).select_from(media_resource)) == 0

    @pytest.mark.asyncio
    async def test_distinct_works_stay_distinct(self, session) -> None:
        """《天命大主宰》不能被并进《大主宰》—— 误并不可逆。"""
        docs = [_doc(1, "大主宰"), _doc(2, "天命大主宰"), _doc(3, "诛天大主宰")]
        session.add_all(docs)
        await session.commit()

        await parse_batch(session, docs, RuleExtractor())
        await session.commit()

        keys = sorted(await session.scalars(select(Work.norm_key)))
        assert keys == ["大主宰", "天命大主宰", "诛天大主宰"]

    @pytest.mark.asyncio
    async def test_type_disagreement_no_longer_splits_a_work(self, session) -> None:
        """旧身份三元组里有 `media_type`，anime/tv 分歧就裂成两行。

        现在 media 的身份是 `(work_id, season)`，类型退化成属性 —— 所以
        原来那套「类型放宽」回退删掉之后，分歧也不再有裂开的途径。
        """
        docs = [_doc(1, "分歧剧 电视剧"), _doc(2, "分歧剧 动漫")]
        session.add_all(docs)
        await session.commit()

        await parse_batch(session, docs, RuleExtractor())
        await session.commit()

        assert await session.scalar(select(func.count()).select_from(Work)) == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 1

    @pytest.mark.asyncio
    async def test_rerunning_the_same_document_is_idempotent(self, session) -> None:
        """重跑 parse 不能把并好的东西拆开 —— 这是 `title_canon` 存在的首要理由。"""
        doc = _doc(1, "大主宰 第二季")
        session.add(doc)
        await session.commit()

        await parse_document(session, doc, RuleExtractor())
        await session.commit()
        doc.parse_status = ParseStatus.PENDING
        await session.commit()
        await parse_document(session, doc, RuleExtractor())
        await session.commit()

        assert await session.scalar(select(func.count()).select_from(Work)) == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 1
        assert await session.scalar(select(func.count()).select_from(TitleCanon)) == 1

    @pytest.mark.asyncio
    async def test_no_season_rows_use_the_sentinel_not_null(self, session) -> None:
        """`season` 在迁移 B 里是 NOT NULL 且参与唯一索引，不能留 NULL。"""
        doc = _doc(1, "一部电影")
        session.add(doc)
        await session.commit()

        await parse_document(session, doc, RuleExtractor())
        await session.commit()

        media = await session.scalar(select(Media))
        assert media.season == NO_SEASON
        assert media.work_id is not None
