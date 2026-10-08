"""归一流程：垃圾清理、确定性建 Work、季合并。

这三步会**不可逆地删改几十万行生产数据**，所以每一条断言针对的都是
「跑错了会丢数据」的那种失败，而不是接口形状：

- `purge` 不能碰 resource 行（链接是真实采集成本）
- `merge_media_rows` 不能丢资源关联，也不能因为两个败者挂着同一条资源
  而撞关联表主键
- `rebuild` 必须把同一部剧的噪声变体收进一个 Work，同时**不能**把
  《天命大主宰》这种另一部作品并进来 —— 误并不可逆，漏并还能再跑
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from sqlalchemy import func, select

from funflix.base.enums import MediaType, Provider, Quality
from funflix.models import Media, Resource, Tag, TagKind, Work, media_resource, media_tag, utcnow
from funflix.models.canon import CanonState, TitleCanon
from funflix.models.media import NO_SEASON, UNKNOWN_YEAR
from funflix.services.canon import (
    apply_canon_decisions,
    assign_identities,
    merge_media_rows,
    purge_junk_media,
    rebuild_works,
)
from funflix.services.canon.merge import absorb_attributes, pick_survivor
from funflix.services.canon.purge import is_junk_media_title
from funflix.services.canon.resolver import (
    CanonDecision,
    CanonEntry,
    _persist,
    validate_decisions,
)
from funflix.services.counters import (
    refresh_counters_for_media,
    refresh_media_counters,
    refresh_work_counters,
)
from funflix.services.text.normalize import series_norm_key

#: 给每行夹具造一个独占的「旧归属」Work，norm_key 必须互不相同（它是唯一键）。
_STALE = itertools.count()


def _media(title: str, **kw) -> Media:
    """一行待归一的 media，挂在一个**按旧规则分组**的 Work 上。

    迁移 B 之后 `media.work_id` 是 NOT NULL —— 「还没归属」这个状态在库里
    不再表示得出来。所以夹具还原的是收口之后的真实处境：每行都有归属，
    只是分组是错的（每条分享自己一个 Work，等于完全没归并）。
    `rebuild` 要做的正是重新分组，这比「从 NULL 填上」是更强的断言。

    于是 `Work` 的总数会**大于**正确分组数 —— 被腾空的旧 Work 刻意不删
    （审计需要，见 `canon/apply.py` 的模块说明）。要数「真实作品」请用
    `_live_works`，别数 `select(Work)`。

    显式传了 `work` / `work_id` 的话就用调用方给的归属，不再造旧 Work。
    """
    kw.setdefault("norm_key", title)
    kw.setdefault("media_type", MediaType.UNKNOWN)
    kw.setdefault("year", UNKNOWN_YEAR)
    kw.setdefault("aliases", [])
    kw.setdefault("season", NO_SEASON)
    if "work" not in kw and "work_id" not in kw:
        kw["work"] = Work(
            title=title,
            norm_key=f"stale-{next(_STALE)}-{title}",
            aliases=[],
            media_type=kw["media_type"],
            year=kw["year"],
        )
    return Media(title=title, **kw)


async def _live_works(session) -> list[Work]:
    """还挂着 media 的 Work，按标题排序。

    腾空的旧 Work 留在库里是有意的，所以「归一出几部作品」只能这么数。
    """
    return list(
        await session.scalars(
            select(Work).where(Work.id.in_(select(Media.work_id))).order_by(Work.title)
        )
    )


async def _work_by_key(session, title: str) -> Work | None:
    """按归一键取 Work。`None` 表示这个键还没有对应的作品。"""
    return await session.scalar(select(Work).where(Work.norm_key == series_norm_key(title)))


def _resource(n: int) -> Resource:
    now = utcnow()
    return Resource(
        provider=Provider.QUARK,
        share_id=f"s{n:06d}",
        url=f"https://pan.quark.cn/s/s{n:06d}",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
    )


async def _link(session, media: Media, resource: Resource) -> None:
    await session.execute(
        media_resource.insert().values(
            media_id=media.id, resource_id=resource.id, created_at=utcnow()
        )
    )


async def _attach(session, pairs: list[tuple[Media, list[Resource]]]) -> None:
    """建关联**并刷季级计数**，模拟生产库的真实状态。

    `refresh_work_counters` 是把 `media.resource_count` 往上滚一层
    （见它的文档：「先刷季、再刷作品」），而那一列在生产库里由抽取流程维护。
    测试里光插关联表、不刷这一列，作品级计数就会算成 0 —— 那是 fixture
    没还原生产状态，不是归并逻辑的 bug。
    """
    for media, resources in pairs:
        for resource in resources:
            await _link(session, media, resource)
    await session.commit()
    await refresh_media_counters(session, [m.id for m, _r in pairs])
    await session.commit()


class TestJunkDetection:
    """`is_junk_media_title` 是删除判据，误判一条就少一部作品。"""

    @pytest.mark.parametrize(
        "title",
        [
            "夸克",
            "查看资源",
            "磁力下载",
            "提取码",
            "84",
            "",
            "大",
            "夸克 6 5 2 338 清爽版 apk",
            "夸克 :https: pan quark cn s 00ab4389973f",
        ],
    )
    def test_scrape_artifacts_are_junk(self, title: str) -> None:
        assert is_junk_media_title(title) is True

    @pytest.mark.parametrize(
        "title",
        ["大主宰", "天命大主宰", "美国狙击手", "万岁", "K歌情人", "4K先生", "少年江湖"],
    )
    def test_real_titles_survive(self, title: str) -> None:
        assert is_junk_media_title(title) is False

    def test_none_is_junk(self) -> None:
        """`media.title` 理论上 NOT NULL，但扫描路径要能吃下 None 而不是崩。"""
        assert is_junk_media_title(None) is True


class TestPurgeKeepsResources:
    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, session) -> None:
        session.add_all([_media("夸克"), _media("大主宰")])
        await session.commit()

        report = await purge_junk_media(session)
        assert report.junk == 1
        assert report.deleted == 0
        assert report.dry_run is True
        assert await session.scalar(select(func.count()).select_from(Media)) == 2

    @pytest.mark.asyncio
    async def test_apply_deletes_media_but_never_resources(self, session) -> None:
        """**最重要的一条**：链接是真实采集成本，归属错了不等于链接是假的。"""
        junk, real = _media("查看资源"), _media("大主宰")
        res = _resource(1)
        session.add_all([junk, real, res])
        await session.flush()
        await _link(session, junk, res)
        await _link(session, real, res)
        await session.commit()

        report = await purge_junk_media(session, dry_run=False)
        assert report.deleted == 1
        assert report.links_detached == 1

        titles = set(await session.scalars(select(Media.title)))
        assert titles == {"大主宰"}
        # resource 行还在，而且它跟真作品的关联也还在
        assert await session.scalar(select(func.count()).select_from(Resource)) == 1
        remaining = list(await session.scalars(select(media_resource.c.media_id)))
        assert remaining == [real.id]

    @pytest.mark.asyncio
    async def test_tag_counts_are_recomputed(self, session) -> None:
        """删掉垃圾行之后标签计数必须跟着降，否则筛选页会显示空结果。"""
        junk = _media("磁力下载")
        tag = Tag(name="夸克", norm_key="夸克", kind=TagKind.OTHER, media_count=1)
        session.add_all([junk, tag])
        await session.flush()
        await session.execute(
            media_tag.insert().values(media_id=junk.id, tag_id=tag.id, created_at=utcnow())
        )
        await session.commit()

        report = await purge_junk_media(session, dry_run=False)
        assert report.tags_detached == 1
        assert report.tags_recounted == 1
        await session.refresh(tag)
        assert tag.media_count == 0

    @pytest.mark.asyncio
    async def test_work_counts_are_recomputed(self, session) -> None:
        """删掉垃圾行之后它所属 Work 的季数也必须跟着降。

        回归：真库上跑完整条流水线之后，剩下一个「声称有 1 季、实际 0 季」的
        Work（`#大主宰 #leoziyuan #动画 #动作冒险`）—— 它唯一的那行 media 被
        `apply` 的 junk 路径删了，而删除路径当时只重算标签、不重算作品。
        行删掉之后就再也查不出它曾属于哪部作品，所以这件事只能在删之前记下
        归属、由删除函数自己收尾。
        """
        junk = _media("磁力下载")
        session.add(junk)
        await session.flush()
        work_id = junk.work_id
        work = await session.get(Work, work_id)
        assert work is not None
        work.season_count, work.resource_count = 1, 7
        await session.commit()

        report = await purge_junk_media(session, dry_run=False)
        assert report.deleted == 1
        assert report.works_recounted == 1
        await session.refresh(work)
        assert (work.season_count, work.resource_count) == (0, 0)
        assert await session.get(Work, work_id) is not None, "空 Work 不删，只把计数刷成 0"

    @pytest.mark.asyncio
    async def test_limit_caps_deletions_but_not_the_count(self, session) -> None:
        """`--limit` 是小步试探用的闸，报告里仍要看到全表命中数。"""
        session.add_all([_media("夸克"), _media("链接"), _media("磁力下载")])
        await session.commit()

        report = await purge_junk_media(session, dry_run=False, limit=2)
        assert report.junk == 3
        assert report.deleted == 2
        assert await session.scalar(select(func.count()).select_from(Media)) == 1


class TestMergeMediaRows:
    @pytest.mark.asyncio
    async def test_survivor_is_the_one_with_most_resources(self, session) -> None:
        thin, fat = _media("大主宰 S02"), _media("大主宰 第2季")
        resources = [_resource(i) for i in range(3)]
        session.add_all([thin, fat, *resources])
        await session.flush()
        await _link(session, thin, resources[0])
        await _link(session, fat, resources[1])
        await _link(session, fat, resources[2])
        await session.commit()

        assert await pick_survivor(session, [thin.id, fat.id]) == fat.id

    @pytest.mark.asyncio
    async def test_two_losers_sharing_a_resource_do_not_collide(self, session) -> None:
        """两个败者挂着同一条资源 —— 一条 UPDATE 全迁会撞关联表主键。

        这是 `_move_links` 必须逐个败者迁的原因，也是真实数据里的常见形态
        （同一个链接被不同分享者各发一遍，解析成两行 media）。
        """
        survivor, a, b = _media("大主宰"), _media("大主宰 年番2"), _media("大主宰 S02")
        shared, only_survivor = _resource(1), _resource(2)
        session.add_all([survivor, a, b, shared, only_survivor])
        await session.flush()
        await _link(session, survivor, only_survivor)
        await _link(session, a, shared)
        await _link(session, b, shared)
        await session.commit()

        # 显式指定存活行：这条测的是关联迁移不撞主键，不是存活行怎么挑
        # （三行各挂一条资源，`pick_survivor` 在这里是平票）。
        kept, stats = await merge_media_rows(
            session, [survivor.id, a.id, b.id], survivor_id=survivor.id
        )
        await session.commit()

        assert kept == survivor.id
        assert stats.merged == 2
        # shared 被迁了一次，第二次是重复所以丢弃
        assert stats.links_moved == 1
        assert stats.links_dropped == 1
        pairs = set(
            (
                await session.execute(
                    select(media_resource.c.media_id, media_resource.c.resource_id)
                )
            ).all()
        )
        assert pairs == {(survivor.id, only_survivor.id), (survivor.id, shared.id)}
        assert await session.scalar(select(func.count()).select_from(Media)) == 1
        # 资源一条都没丢
        assert await session.scalar(select(func.count()).select_from(Resource)) == 2

    @pytest.mark.asyncio
    async def test_loser_titles_become_aliases(self, session) -> None:
        survivor, loser = _media("大主宰"), _media("大主宰 动漫版")
        session.add_all([survivor, loser])
        await session.flush()
        await session.commit()

        await merge_media_rows(session, [survivor.id, loser.id], survivor_id=survivor.id)
        await session.commit()
        assert "大主宰 动漫版" in survivor.aliases

    @pytest.mark.asyncio
    async def test_single_row_is_a_no_op(self, session) -> None:
        lone = _media("大主宰")
        session.add(lone)
        await session.commit()

        kept, stats = await merge_media_rows(session, [lone.id])
        assert kept == lone.id
        assert stats.merged == 0

    @pytest.mark.asyncio
    async def test_a_loser_that_vanished_is_skipped(self, session) -> None:
        """并的是快照：别的节点可能先把其中几行删了。

        CI 里 canon 和 repair 是两个并行 job，canon merge 删 media 行，而
        repair apply 用的是几十分钟前 scan 算出来的计划。实测这里抛了
        `KeyError`、一路冒到 CLI，`repair apply` 整步退出 1，
        **那一轮其余已经并好的组也跟着回滚**。
        """
        survivor, alive_loser = _media("大主宰"), _media("大主宰 年番")
        session.add_all([survivor, alive_loser])
        await session.flush()
        await session.commit()
        vanished = uuid.uuid4()  # 并到一半被别的节点删掉的那一行

        kept, stats = await merge_media_rows(
            session, [survivor.id, alive_loser.id, vanished], survivor_id=survivor.id
        )
        await session.commit()

        assert kept == survivor.id
        # 还在的那个照并，没了的那个跳过
        assert stats.merged == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 1

    @pytest.mark.asyncio
    async def test_a_vanished_survivor_cancels_the_whole_group(self, session) -> None:
        """存活行自己被删了就整组不并 —— 换个存活行等于换上游的结论。

        这一组的身份归属是 `assign._pass` 按「目标身份上坐着哪一行」算出来的，
        那一行没了，就该让下一轮 scan 重新看现在是谁坐在那儿。
        """
        loser_a, loser_b = _media("大主宰 年番"), _media("大主宰 S02")
        session.add_all([loser_a, loser_b])
        await session.flush()
        await session.commit()
        vanished_survivor = uuid.uuid4()

        kept, stats = await merge_media_rows(
            session,
            [vanished_survivor, loser_a.id, loser_b.id],
            survivor_id=vanished_survivor,
        )
        await session.commit()

        assert kept == vanished_survivor
        assert stats.merged == 0
        # 败者原样留着，等下一轮重新规划
        assert await session.scalar(select(func.count()).select_from(Media)) == 2


class TestAbsorbAttributes:
    def test_year_takes_the_earliest_known(self) -> None:
        """同一季的不同分享里年份一个写首播年一个写引进年，首播年才对。"""
        survivor, loser = _media("x", year=2025), _media("x", year=2023)
        absorb_attributes(survivor, loser)
        assert survivor.year == 2023

    def test_unknown_year_is_filled_not_compared(self) -> None:
        """0 是「未知」哨兵，不是「公元 0 年」—— 不能参与取小。"""
        survivor, loser = _media("x", year=UNKNOWN_YEAR), _media("x", year=2023)
        absorb_attributes(survivor, loser)
        assert survivor.year == 2023

    def test_conflicting_known_types_are_left_alone(self) -> None:
        """`anime` vs `tv` 的分歧要 LLM 裁决，在这里随便挑一个是把错误固化。"""
        survivor = _media("x", media_type=MediaType.TV)
        loser = _media("x", media_type=MediaType.ANIME)
        absorb_attributes(survivor, loser)
        assert survivor.media_type is MediaType.TV

    def test_unknown_type_is_filled(self) -> None:
        survivor = _media("x", media_type=MediaType.UNKNOWN)
        loser = _media("x", media_type=MediaType.ANIME)
        absorb_attributes(survivor, loser)
        assert survivor.media_type is MediaType.ANIME

    def test_survivor_title_is_not_duplicated_into_aliases(self) -> None:
        survivor, loser = _media("大主宰"), _media("大主宰")
        absorb_attributes(survivor, loser)
        assert survivor.aliases == []


class TestRebuildWorks:
    @pytest.mark.asyncio
    async def test_noise_variants_collapse_into_one_work(self, session) -> None:
        variants = [
            _media("大主宰"),
            _media("大主宰 4K高码"),
            _media("国漫《大主宰》更至03"),
            _media("大主宰 American Overlord"),
        ]
        resources = [_resource(i) for i in range(len(variants))]
        session.add_all([*variants, *resources])
        await session.flush()
        # 每行各挂一条资源 —— 合并走的是 `refresh_media_counters`，而它的契约
        # 是「顺手删掉没有资源的 media」，全空的话存活行也会被删（见
        # `rebuild._recount` 的说明）。生产库里每行 media 都至少有一条资源。
        for media, resource in zip(variants, resources, strict=True):
            await _link(session, media, resource)
        await session.commit()

        report = await rebuild_works(session, dry_run=False)
        assert report.works_created == 1
        works = await _live_works(session)
        assert [w.title for w in works] == ["大主宰"]
        # 四行都挂到同一个 Work，而且因为季号都拿不准，全落 NO_SEASON
        # 并被合成一行 —— 四条资源一条不少地挂到存活行上
        assert await session.scalar(select(func.count()).select_from(Media)) == 1
        assert works[0].resource_count == len(resources)

    @pytest.mark.asyncio
    async def test_different_works_stay_apart(self, session) -> None:
        """**误并不可逆** —— 这几部是真的不同作品，一条都不能并进《大主宰》。"""
        session.add_all(
            [
                _media("大主宰"),
                _media("天命大主宰"),
                _media("诛天大主宰"),
                _media("北灵少年志之大主宰"),
                _media("深空彼岸大主宰4"),
            ]
        )
        await session.commit()

        await rebuild_works(session, dry_run=False)
        titles = {w.title for w in await _live_works(session)}
        assert titles == {
            "大主宰",
            "天命大主宰",
            "诛天大主宰",
            "北灵少年志之大主宰",
            "深空彼岸大主宰4",
        }

    @pytest.mark.asyncio
    async def test_high_confidence_seasons_become_separate_rows(self, session) -> None:
        """季是子层，不是归并目标：第 1 季和第 2 季的链接不能混在一起。"""
        session.add_all([_media("大主宰 第1季"), _media("大主宰 第2季"), _media("大主宰")])
        await session.commit()

        await rebuild_works(session, dry_run=False)
        (work,) = await _live_works(session)
        seasons = sorted(
            await session.scalars(select(Media.season).where(Media.work_id == work.id))
        )
        assert seasons == [NO_SEASON, 1, 2]

    @pytest.mark.asyncio
    async def test_dry_run_creates_no_work(self, session) -> None:
        session.add_all([_media("大主宰"), _media("大主宰 4K高码")])
        await session.commit()

        report = await rebuild_works(session)
        assert report.keys == 1
        assert report.works_created == 0
        # 库里只剩夹具造的那两个「旧归属」，归一键对应的 Work 一个都没建
        assert await _work_by_key(session, "大主宰") is None
        assert [m.season for m in await session.scalars(select(Media))] == [NO_SEASON] * 2

    @pytest.mark.asyncio
    async def test_junk_rows_do_not_become_works(self, session) -> None:
        """没跑过 purge 的库上也不能凭空造出垃圾 Work。"""
        session.add_all([_media("夸克"), _media("查看资源"), _media("大主宰")])
        await session.commit()

        report = await rebuild_works(session, dry_run=False)
        assert report.skipped_junk == 2
        assert report.works_created == 1

    @pytest.mark.asyncio
    async def test_merged_row_without_resources_is_dropped(self, session) -> None:
        """并完一条资源都没有的季会被删掉，Work 如实留在 0 季。

        这是 `refresh_media_counters` 的既有契约（「顺手删掉没有资源的作品」）
        在归并路径上的表现，钉住它免得以后有人以为是 bug 而"修"掉 ——
        资源为零的季既搜不出东西也点不开。
        """
        session.add_all([_media("大主宰"), _media("大主宰 4K高码")])
        await session.commit()

        await rebuild_works(session, dry_run=False)
        assert await session.scalar(select(func.count()).select_from(Media)) == 0
        work = await _work_by_key(session, "大主宰")
        assert work is not None
        assert work.season_count == 0

    @pytest.mark.asyncio
    async def test_key_scoping_touches_nothing_else(self, session) -> None:
        """单组演练必须真的只动那一组 —— 这是放开全库前唯一的安全闸。"""
        outsider = _media("凡人修仙传")
        session.add_all([_media("大主宰"), _media("大主宰 4K高码"), outsider])
        await session.commit()
        stale_home = outsider.work_id

        await rebuild_works(session, dry_run=False, key=series_norm_key("大主宰"))
        assert await _work_by_key(session, "大主宰") is not None
        # 组外那一行连 Work 都没给它建，归属和标题都还是夹具里那个旧的
        assert await _work_by_key(session, "凡人修仙传") is None
        untouched = await session.scalar(select(Media).where(Media.title == "凡人修仙传"))
        assert untouched.work_id == stale_home

    @pytest.mark.asyncio
    async def test_rerun_is_idempotent(self, session) -> None:
        """可中断续跑的前提：同样的输入跑两遍不能造出第二个 Work。"""
        a, b = _media("大主宰"), _media("大主宰 4K高码")
        resources = [_resource(0), _resource(1)]
        session.add_all([a, b, *resources])
        await session.flush()
        await _link(session, a, resources[0])
        await _link(session, b, resources[1])
        await session.commit()

        await rebuild_works(session, dry_run=False)
        second = await rebuild_works(session, dry_run=False)
        assert second.works_created == 0
        assert second.works_existing == 1
        # 第二趟一行都不该搬 —— 大家已经坐在正确的 `(work_id, season)` 上了
        assert second.media_updated == 0
        assert second.media_merged == 0
        assert len(await _live_works(session)) == 1

    @pytest.mark.asyncio
    async def test_decided_keys_are_left_to_apply(self, session) -> None:
        """已裁决的键重跑时必须原样不动 —— 否则规则会把 LLM 的判定推翻。

        回归：真库上跑完 `canon merge` 再跑一遍 `canon rebuild`，三本小说
        被从 `book` 作品里拽回规则算出来的键上（`大主宰:我荒古圣体…作者:墨之
        所想` 又成了一部独立"作品"），`canon merge` 再跑又搬回去 —— 两个阶段
        来回拉锯，而付过的 token 白付。`title_canon` 的全部意义就是不回退。
        """
        novel = _media("大主宰:我荒古圣体,当为天帝! 作者:墨之所想")
        resource = _resource(0)
        session.add_all([novel, resource])
        await session.flush()
        await _link(session, novel, resource)
        # LLM 判过了：这是小说，归到自己的作品下
        decided = Work(
            title="大主宰（小说）",
            norm_key="大主宰小说",
            aliases=[],
            media_type=MediaType.BOOK,
            year=UNKNOWN_YEAR,
        )
        session.add(decided)
        await session.flush()
        novel.work_id = decided.id
        session.add(
            TitleCanon(
                norm_key=series_norm_key(novel.title),
                work_norm_key="大主宰小说",
                work_title="大主宰（小说）",
                season=None,
                media_type=MediaType.BOOK,
                year=UNKNOWN_YEAR,
                status=CanonState.DECIDED,
            )
        )
        await session.commit()

        report = await rebuild_works(session, dry_run=False)

        assert report.skipped_decided == 1
        assert report.works_created == 0, "不该为已裁决的键造规则版 Work"
        await session.refresh(novel)
        assert novel.work_id == decided.id, "裁决被规则推翻了"
        assert await _work_by_key(session, novel.title) is None

    @pytest.mark.asyncio
    async def test_pending_keys_are_still_rebuilt(self, session) -> None:
        """只有 `decided` 才算裁决。pending 是"还没判"，规则照常分组。"""
        media = _media("大主宰")
        resource = _resource(0)
        session.add_all([media, resource])
        await session.flush()
        await _link(session, media, resource)
        session.add(TitleCanon(norm_key=series_norm_key("大主宰"), status=CanonState.PENDING))
        await session.commit()

        report = await rebuild_works(session, dry_run=False)

        assert report.skipped_decided == 0
        assert report.works_created == 1

    @pytest.mark.asyncio
    async def test_resources_survive_the_whole_rebuild(self, session) -> None:
        a, b = _media("大主宰"), _media("大主宰 4K高码")
        resources = [_resource(i) for i in range(3)]
        session.add_all([a, b, *resources])
        await session.flush()
        await _link(session, a, resources[0])
        await _link(session, b, resources[1])
        await _link(session, b, resources[2])
        await session.commit()

        await rebuild_works(session, dry_run=False)
        assert await session.scalar(select(func.count()).select_from(Resource)) == 3
        (work,) = await _live_works(session)
        assert work.season_count == 1
        assert work.resource_count == 3


class TestRefreshWorkCounters:
    @pytest.mark.asyncio
    async def test_rolls_up_from_season_counters(self, session) -> None:
        work = Work(
            title="大主宰", norm_key="大主宰", aliases=[], media_type=MediaType.ANIME, year=0
        )
        session.add(work)
        await session.flush()
        s1 = _media("大主宰 第1季", work_id=work.id, season=1)
        s2 = _media("大主宰 第2季", work_id=work.id, season=2)
        resources = [_resource(i) for i in range(3)]
        session.add_all([s1, s2, *resources])
        await session.flush()
        await _link(session, s1, resources[0])
        await _link(session, s2, resources[1])
        await _link(session, s2, resources[2])
        await session.commit()

        # 必须先刷季级 —— 作品级是从 media 的冗余列汇总上来的
        await refresh_media_counters(session, [s1.id, s2.id])
        await refresh_work_counters(session, [work.id])
        await session.commit()

        await session.refresh(work)
        assert work.season_count == 2
        assert work.resource_count == 3

    @pytest.mark.asyncio
    async def test_work_without_seasons_is_zeroed_not_deleted(self, session) -> None:
        """空作品是归并中间态（季被挪走了），不是「这部剧没了」。"""
        work = Work(
            title="大主宰",
            norm_key="大主宰",
            aliases=[],
            media_type=MediaType.ANIME,
            year=0,
            season_count=2,
            resource_count=9,
        )
        session.add(work)
        await session.commit()

        await refresh_work_counters(session, [work.id])
        await session.commit()

        await session.refresh(work)
        assert work.season_count == 0
        assert work.resource_count == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 1

    @pytest.mark.asyncio
    async def test_combined_entry_rolls_both_levels(self, session) -> None:
        """`refresh_counters_for_media` 必须把两级一起刷上。

        搜索列表读的是 `Work` 上的计数，而抽取/清理/校验那几条路径手里只有
        media_id。只刷季不往上滚一层的话，新入库的资源在搜索结果里看不见，
        而且不会报任何错 —— 和 `valid_resource_count` 当年没有写入点是同一类缺陷。
        """
        work = Work(
            title="大主宰", norm_key="大主宰", aliases=[], media_type=MediaType.ANIME, year=0
        )
        session.add(work)
        await session.flush()
        season = _media("大主宰 第1季", work_id=work.id, season=1)
        resources = [_resource(i) for i in range(2)]
        session.add_all([season, *resources])
        await session.flush()
        for r in resources:
            await _link(session, season, r)
        await session.commit()

        assert await refresh_counters_for_media(session, [season.id]) == 1
        await session.commit()

        await session.refresh(season)
        await session.refresh(work)
        assert (season.resource_count, season.valid_resource_count) == (2, 0)
        assert (work.season_count, work.resource_count) == (1, 2)

    @pytest.mark.asyncio
    async def test_combined_entry_zeroes_the_work_when_its_last_season_dies(self, session) -> None:
        """季被物理删掉时，作品归属必须在删之前问出来。

        `refresh_media_counters` 的契约是顺手删掉零资源的季 —— 行没了就再也
        查不到它曾属于哪部作品，作品计数会永远停在旧值上。
        """
        work = Work(
            title="大主宰",
            norm_key="大主宰",
            aliases=[],
            media_type=MediaType.ANIME,
            year=0,
            season_count=1,
            resource_count=5,
        )
        session.add(work)
        await session.flush()
        season = _media("大主宰 第1季", work_id=work.id, season=1)
        session.add(season)
        await session.commit()

        await refresh_counters_for_media(session, [season.id])
        await session.commit()

        assert await session.get(Media, season.id) is None, "零资源的季要被删掉"
        await session.refresh(work)
        assert (work.season_count, work.resource_count) == (0, 0)


class TestResolveValidation:
    """`validate_decisions` 是模型输出进库前的唯一闸门。

    模型是概率性的，这里每一条断言对应一种真实会发生的失误 —— 漏答、
    改写 key、重复回答、给个空标题。全都必须被拦下并计数，不能静默进库。
    """

    ENTRIES = [
        CanonEntry(key="大主宰", rows=379, resources=1974, sample="大主宰 年番2"),
        CanonEntry(key="大主宰2", rows=5, resources=11, sample="大主宰2"),
        CanonEntry(key="天命大主宰", rows=35, resources=65, sample="天命大主宰"),
    ]

    def _payload(self, *decisions) -> dict:
        return {"decisions": list(decisions)}

    def _ok(self, key: str, title: str, **kw) -> dict:
        return {
            "key": key,
            "work_title": title,
            "media_type": kw.get("media_type", "anime"),
            "season": kw.get("season"),
            "year": kw.get("year"),
            "is_junk": kw.get("is_junk", False),
            "confidence": kw.get("confidence", 0.9),
        }

    def test_same_work_title_yields_the_same_work_key(self) -> None:
        """`work_norm_key` 本地算，不问模型。

        这是「标题一样、键不一样」那类自相矛盾输出的根治办法 —— 模型只要
        给出字面相同的 `work_title`，归一键就必然相同，并到一起去。
        """
        decisions, _ = validate_decisions(
            self._payload(
                self._ok("大主宰", "大主宰"),
                self._ok("大主宰2", "大主宰", season=2),
            ),
            self.ENTRIES,
        )
        assert len({d.work_norm_key for d in decisions}) == 1

    def test_distinct_works_stay_distinct(self) -> None:
        decisions, _ = validate_decisions(
            self._payload(
                self._ok("大主宰", "大主宰"),
                self._ok("天命大主宰", "天命大主宰"),
            ),
            self.ENTRIES,
        )
        assert len({d.work_norm_key for d in decisions}) == 2

    def test_unknown_key_is_rejected(self) -> None:
        """模型编了一个没送进去的 key —— 进库就是一条永远匹配不上的裁决。"""
        decisions, stats = validate_decisions(
            self._payload(self._ok("斗破苍穹", "斗破苍穹")), self.ENTRIES
        )
        assert decisions == []
        assert stats["unknown_key"] == 1

    def test_rewritten_key_is_rejected(self) -> None:
        """key 必须原样照抄。模型"顺手清洗"过的 key 对不上库里的任何行。"""
        _decisions, stats = validate_decisions(
            self._payload(self._ok("大主宰 年番2", "大主宰")), self.ENTRIES
        )
        assert stats["unknown_key"] == 1

    def test_duplicate_key_keeps_only_the_first(self) -> None:
        decisions, stats = validate_decisions(
            self._payload(
                self._ok("大主宰", "大主宰"),
                self._ok("大主宰", "天命大主宰"),
            ),
            self.ENTRIES,
        )
        assert [d.work_title for d in decisions] == ["大主宰"]
        assert stats["duplicate_key"] == 1

    def test_missing_keys_are_counted_not_invented(self) -> None:
        """漏答的 key 留在 pending，下次重跑 —— 绝不替模型猜一个。"""
        decisions, stats = validate_decisions(
            self._payload(self._ok("大主宰", "大主宰")), self.ENTRIES
        )
        assert len(decisions) == 1
        assert stats["missing"] == 2

    def test_empty_work_title_is_rejected_unless_junk(self) -> None:
        _decisions, stats = validate_decisions(self._payload(self._ok("大主宰", "")), self.ENTRIES)
        assert stats["empty_work_title"] == 1

    def test_all_noise_work_title_is_rejected(self) -> None:
        """模型给的标题清洗完是空的（整条都是噪声词）—— 当不了作品身份。"""
        _decisions, stats = validate_decisions(
            self._payload(self._ok("大主宰", "4K高码")), self.ENTRIES
        )
        assert stats["empty_work_title"] == 1

    def test_junk_decision_needs_no_title(self) -> None:
        decisions, stats = validate_decisions(
            self._payload(self._ok("大主宰2", None, is_junk=True)), self.ENTRIES
        )
        assert stats["empty_work_title"] == 0
        assert decisions[0].is_junk is True
        assert decisions[0].work_norm_key is None

    @pytest.mark.parametrize("bad", [1899, 2101, "2024", True, None, 3.5])
    def test_out_of_range_year_becomes_unknown(self, bad) -> None:
        decisions, _ = validate_decisions(
            self._payload(self._ok("大主宰", "大主宰", year=bad)), self.ENTRIES
        )
        assert decisions[0].year == UNKNOWN_YEAR

    @pytest.mark.parametrize("bad", [-1, 100, "2", True, 2.5])
    def test_out_of_range_season_becomes_none_not_zero(self, bad) -> None:
        """越界季号只能折成 None，**不能**折成 0。

        0 的意思是「确定无季概念」，会覆盖规则逐行判出的季号；None 的意思是
        「这个键没锁定某一季」，不动那些季号。把越界值当 0 会把一部剧的
        所有季压成一行 —— 正是这次改造要消掉的毛病。
        """
        decisions, _ = validate_decisions(
            self._payload(self._ok("大主宰", "大主宰", season=bad)), self.ENTRIES
        )
        assert decisions[0].season is None

    def test_unknown_media_type_falls_back_instead_of_raising(self) -> None:
        decisions, _ = validate_decisions(
            self._payload(self._ok("大主宰", "大主宰", media_type="国漫")), self.ENTRIES
        )
        assert decisions[0].media_type is MediaType.UNKNOWN

    def test_book_type_survives(self) -> None:
        """小说条目要保留成 book，不是删掉也不是当成那部动漫。"""
        decisions, _ = validate_decisions(
            self._payload(self._ok("大主宰2", "大主宰我荒古圣体当为天帝", media_type="book")),
            self.ENTRIES,
        )
        assert decisions[0].media_type is MediaType.BOOK

    def test_garbage_payload_yields_nothing(self) -> None:
        decisions, stats = validate_decisions({"decisions": "nope"}, self.ENTRIES)
        assert decisions == []
        assert stats["missing"] == 3


class TestPersistDecisions:
    """落库那一步。`validate_decisions` 只保证**块内**的 key 不重复。"""

    def _decision(self, key: str, title: str, confidence: float) -> CanonDecision:
        return CanonDecision(
            key=key,
            work_norm_key=series_norm_key(title),
            work_title=title,
            season=None,
            media_type=MediaType.MOVIE,
            year=UNKNOWN_YEAR,
            is_junk=False,
            confidence=confidence,
        )

    @pytest.mark.asyncio
    async def test_same_key_from_two_blocks_does_not_violate_the_primary_key(self, session) -> None:
        """同一个 key 被两个候选块各裁一遍 —— 不能 `add` 两行同主键。

        `series_norm_key` 会丢掉尾部的拉丁别名而 `block_key` 留着，于是
        《疯狂的外星人》和《疯狂的外星人 Crazy Alien》归一到同一个 key、
        却分进两个块。两块各返一条裁决，而这个 key 在 `title_canon` 里还
        没有行（新采进来的作品就是这样），先查后写那步查不到，commit 就炸
        `UniqueViolationError: pk_title_canon`。生产库里 104,527 个 key
        有 621 个横跨多块，这是必然会踩到的。
        """
        n, junk = await _persist(
            session,
            [
                self._decision("疯狂的外星人", "疯狂的外星人", 0.7),
                self._decision("疯狂的外星人", "疯狂的外星人", 0.9),
            ],
            "test-model",
        )
        assert n == 1
        assert junk == 0
        rows = list(await session.scalars(select(TitleCanon)))
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_the_more_confident_of_two_verdicts_wins(self, session) -> None:
        """两块给出矛盾裁决时按置信度取，不按它们恰好的到达顺序取。"""
        await _persist(
            session,
            [
                self._decision("碧蓝之海", "碧蓝之海 OVA", 0.4),
                self._decision("碧蓝之海", "碧蓝之海", 0.95),
            ],
            "test-model",
        )
        row = await session.scalar(select(TitleCanon))
        assert row.work_title == "碧蓝之海"

    @pytest.mark.asyncio
    async def test_existing_row_is_updated_in_place(self, session) -> None:
        """key 已经有行时是更新，不是插第二行。"""
        session.add(_canon("葬送的芙莉莲", work_title="旧答案", status=CanonState.PENDING))
        await session.commit()

        await _persist(session, [self._decision("葬送的芙莉莲", "葬送的芙莉莲", 0.9)], "test-model")

        rows = list(await session.scalars(select(TitleCanon)))
        assert len(rows) == 1
        assert rows[0].work_title == "葬送的芙莉莲"
        # `CanonState` 是一组字符串常量，不是枚举 —— 只能比值，不能比身份。
        assert rows[0].status == CanonState.DECIDED


def _canon(key: str, **kw) -> TitleCanon:
    """一条已裁决的 `title_canon` 行。"""
    return TitleCanon(
        norm_key=key,
        work_norm_key=kw.get("work_norm_key", kw.get("work_title")),
        work_title=kw.get("work_title"),
        season=kw.get("season"),
        media_type=kw.get("media_type", MediaType.ANIME),
        year=kw.get("year", UNKNOWN_YEAR),
        is_junk=kw.get("is_junk", False),
        status=kw.get("status", CanonState.DECIDED),
    )


class TestApplyCanonDecisions:
    """阶段 4：把裁决落到库上。误应用和误并一样不可逆。"""

    @pytest.mark.asyncio
    async def test_sequel_key_merges_into_the_work_as_its_own_season(self, session) -> None:
        """`大主宰2` 被判成《大主宰》第 2 季 —— 两个 Work 收敛成一个、两季并存。

        这是整条 LLM 链路要解决的核心场景：规则不敢摘 `大主宰2` 末尾那个 2
        （怕把《误杀2》并进《误杀》），模型敢。
        """
        base, sequel = _media("大主宰"), _media("大主宰2")
        resources = [_resource(i) for i in range(2)]
        session.add_all([base, sequel, *resources])
        await session.commit()
        await _attach(session, [(base, [resources[0]]), (sequel, [resources[1]])])

        await rebuild_works(session, dry_run=False)
        # 规则不敢摘 `大主宰2` 末尾的 2，于是先分成两部作品 —— 这正是下面
        # 那条裁决要修的局面
        assert [w.title for w in await _live_works(session)] == ["大主宰", "大主宰2"]

        session.add(_canon("大主宰2", work_title="大主宰", season=2))
        await session.commit()

        report = await apply_canon_decisions(session, dry_run=False)
        assert report.media_rehomed == 1
        assert report.seasons_overridden == 1

        work = await session.scalar(select(Work).where(Work.norm_key == "大主宰"))
        seasons = sorted(
            await session.scalars(select(Media.season).where(Media.work_id == work.id))
        )
        assert seasons == [NO_SEASON, 2]
        assert work.season_count == 2
        assert work.resource_count == 2

    @pytest.mark.asyncio
    async def test_null_season_does_not_flatten_existing_seasons(self, session) -> None:
        """`season=None` 的裁决**不能**把已有的季压成一行。

        None 的意思是「这个键没锁定某一季」。把它当 0 处理会让第 1 季和
        第 2 季撞进同一个 `(work_id, season)` 然后被合并 —— 一部剧的两季
        资源混在一行上，不可逆。
        """
        rows = [_media("大主宰 第一季"), _media("大主宰 第二季")]
        resources = [_resource(i) for i in range(2)]
        session.add_all([*rows, *resources])
        await session.commit()
        await _attach(session, [(r, [res]) for r, res in zip(rows, resources, strict=True)])

        await rebuild_works(session, dry_run=False)
        session.add(_canon("大主宰", work_title="大主宰", season=None))
        await session.commit()

        report = await apply_canon_decisions(session, dry_run=False)
        assert report.seasons_overridden == 0
        assert report.media_merged == 0

        work = await session.scalar(select(Work).where(Work.norm_key == "大主宰"))
        seasons = sorted(
            await session.scalars(select(Media.season).where(Media.work_id == work.id))
        )
        assert seasons == [1, 2]

    @pytest.mark.asyncio
    async def test_junk_decision_deletes_media_but_keeps_resources(self, session) -> None:
        """LLM 判的 junk 要和规则判的 junk 有完全一样的语义：留着 resource 行。"""
        row = _media("大主宰 84")
        resource = _resource(1)
        session.add_all([row, resource])
        await session.commit()
        await _attach(session, [(row, [resource])])

        await rebuild_works(session, dry_run=False)
        session.add(_canon("大主宰84", is_junk=True, work_title=None, work_norm_key=None))
        await session.commit()

        report = await apply_canon_decisions(session, dry_run=False)
        assert report.junk_keys == 1
        assert report.junk_media_deleted == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 0
        assert await session.scalar(select(func.count()).select_from(Resource)) == 1

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, session) -> None:
        row = _media("大主宰2")
        resource = _resource(1)
        session.add_all([row, resource])
        await session.commit()
        await _attach(session, [(row, [resource])])
        await rebuild_works(session, dry_run=False)

        before = await session.scalar(select(Media.work_id).where(Media.id == row.id))
        session.add(_canon("大主宰2", work_title="大主宰", season=2))
        await session.commit()

        report = await apply_canon_decisions(session, dry_run=True)
        assert report.dry_run is True
        assert report.media_rehomed == 1
        assert report.works_created == 0
        assert await session.scalar(select(Media.work_id).where(Media.id == row.id)) == before

    @pytest.mark.asyncio
    async def test_pending_decisions_are_not_applied(self, session) -> None:
        """只应用 `decided` 的。pending 的是还没裁决完的半成品。"""
        row = _media("大主宰2")
        resource = _resource(1)
        session.add_all([row, resource])
        await session.commit()
        await _attach(session, [(row, [resource])])
        await rebuild_works(session, dry_run=False)

        session.add(_canon("大主宰2", work_title="大主宰", season=2, status=CanonState.PENDING))
        await session.commit()

        report = await apply_canon_decisions(session, dry_run=False)
        assert report.decisions == 0

    @pytest.mark.asyncio
    async def test_rerun_is_idempotent(self, session) -> None:
        """跑两遍结果一样 —— 中断续跑的前提。"""
        base, sequel = _media("大主宰"), _media("大主宰2")
        resources = [_resource(i) for i in range(2)]
        session.add_all([base, sequel, *resources])
        await session.commit()
        await _attach(session, [(base, [resources[0]]), (sequel, [resources[1]])])
        await rebuild_works(session, dry_run=False)

        session.add(_canon("大主宰2", work_title="大主宰", season=2))
        await session.commit()

        await apply_canon_decisions(session, dry_run=False)
        first = sorted((m.season, m.resource_count) for m in await session.scalars(select(Media)))
        await apply_canon_decisions(session, dry_run=False)
        second = sorted((m.season, m.resource_count) for m in await session.scalars(select(Media)))
        assert first == second
        assert await session.scalar(select(func.count()).select_from(Resource)) == 2

    @pytest.mark.asyncio
    async def test_colliding_season_merges_and_keeps_every_resource(self, session) -> None:
        """两个键被判成同一部的同一季 —— 必须并成一行且一条资源不丢。"""
        rows = [_media("大主宰 年番2"), _media("大主宰2")]
        resources = [_resource(i) for i in range(3)]
        session.add_all([*rows, *resources])
        await session.commit()
        await _attach(session, [(rows[0], [resources[0], resources[1]]), (rows[1], [resources[2]])])
        await rebuild_works(session, dry_run=False)

        session.add_all(
            [
                _canon(series_norm_key("大主宰 年番2"), work_title="大主宰", season=2),
                _canon("大主宰2", work_title="大主宰", season=2),
            ]
        )
        await session.commit()

        report = await apply_canon_decisions(session, dry_run=False)
        assert report.season_conflicts == 1
        assert report.media_merged == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 1
        assert await session.scalar(select(func.count()).select_from(Resource)) == 3

        work = await session.scalar(select(Work).where(Work.norm_key == "大主宰"))
        assert work.season_count == 1
        assert work.resource_count == 3


class TestAssignIdentities:
    """搬迁原语。`uq_media_season` 在库上，所以这里每一条都是真实约束下的断言。"""

    @staticmethod
    async def _work(session, norm_key: str) -> Work:
        work = Work(
            title=norm_key, norm_key=norm_key, aliases=[], media_type=MediaType.ANIME, year=0
        )
        session.add(work)
        await session.flush()
        return work

    @pytest.mark.asyncio
    async def test_rows_landing_on_one_identity_are_merged(self, session) -> None:
        """同一个目标身份上的多行必须并成一行，而不是撞唯一键。"""
        work = await self._work(session, "大主宰")
        rows = [_media("大主宰 S02"), _media("大主宰 第2季")]
        resources = [_resource(i) for i in range(3)]
        session.add_all([*rows, *resources])
        await session.commit()
        await _attach(session, [(rows[0], [resources[0]]), (rows[1], resources[1:])])

        stats = await assign_identities(
            session, {rows[0].id: (work.id, 2), rows[1].id: (work.id, 2)}
        )
        await session.commit()

        assert stats.conflicts == 1
        assert stats.merged == 1
        # 资源最多的那行存活，三条关联一条不少
        survivor = await session.scalar(select(Media).where(Media.work_id == work.id))
        assert survivor.id == rows[1].id
        assert survivor.season == 2
        links = list(await session.scalars(select(media_resource.c.media_id)))
        assert links == [survivor.id] * 3

    @pytest.mark.asyncio
    async def test_sitting_occupant_becomes_the_survivor(self, session) -> None:
        """目标身份上已经坐着本组一行时就让它存活 —— 省一次搬迁和一次关联迁移。"""
        work = await self._work(session, "大主宰")
        seated = _media("大主宰 第2季", work_id=work.id, season=2)
        incoming = _media("大主宰 年番2")
        resources = [_resource(i) for i in range(2)]
        session.add_all([seated, incoming, *resources])
        await session.commit()
        # 资源更多的是 incoming —— 但"已就座"优先于"资源最多"
        await _attach(session, [(seated, [resources[0]]), (incoming, [resources[1]])])

        stats = await assign_identities(
            session, {seated.id: (work.id, 2), incoming.id: (work.id, 2)}
        )
        await session.commit()

        assert stats.moved == 0
        assert stats.merged == 1
        survivor = await session.scalar(select(Media).where(Media.work_id == work.id))
        assert survivor.id == seated.id

    @pytest.mark.asyncio
    async def test_stranger_on_the_target_absorbs_the_group(self, session) -> None:
        """目标身份上坐着一行不在搬迁名单里的 media，整组并进它，不能把它挤掉。"""
        work = await self._work(session, "大主宰")
        stranger = _media("大主宰 第2季", work_id=work.id, season=2)
        incoming = _media("大主宰 年番2")
        resources = [_resource(i) for i in range(2)]
        session.add_all([stranger, incoming, *resources])
        await session.commit()
        await _attach(session, [(stranger, [resources[0]]), (incoming, [resources[1]])])

        stats = await assign_identities(session, {incoming.id: (work.id, 2)})
        await session.commit()

        assert stats.merged == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 1
        assert await session.get(Media, stranger.id) is not None

    @pytest.mark.asyncio
    async def test_swap_is_resolved_not_deferred(self, session) -> None:
        """A↔B 换位：先搬谁都撞唯一键，必须靠临时停车位解开。

        这是 `assign.py` 存在的核心理由。换个顺序救不了（成环），
        所以如果哪天停车那段被"简化"掉了，这条会在 `uq_media_season` 上炸。
        """
        work = await self._work(session, "大主宰")
        a = _media("大主宰 第1季", work_id=work.id, season=1)
        b = _media("大主宰 第2季", work_id=work.id, season=2)
        resources = [_resource(i) for i in range(2)]
        session.add_all([a, b, *resources])
        await session.commit()
        await _attach(session, [(a, [resources[0]]), (b, [resources[1]])])

        stats = await assign_identities(session, {a.id: (work.id, 2), b.id: (work.id, 1)})
        await session.commit()

        assert stats.parked > 0
        assert stats.merged == 0, "换位不是合并 —— 两行都得留着"
        await session.refresh(a)
        await session.refresh(b)
        assert (a.season, b.season) == (2, 1)
        # 停车用的负季号不能漏到提交之后
        assert (
            await session.scalar(select(func.count()).select_from(Media).where(Media.season < 0))
            == 0
        )

    @pytest.mark.asyncio
    async def test_three_way_rotation_is_resolved(self, session) -> None:
        """长度 3 的环同样要能解开 —— 两趟收敛不依赖环的长度。"""
        work = await self._work(session, "大主宰")
        rows = [_media(f"大主宰 第{n}季", work_id=work.id, season=n) for n in (1, 2, 3)]
        resources = [_resource(i) for i in range(3)]
        session.add_all([*rows, *resources])
        await session.commit()
        await _attach(session, [(r, [res]) for r, res in zip(rows, resources, strict=True)])

        stats = await assign_identities(
            session,
            {rows[0].id: (work.id, 2), rows[1].id: (work.id, 3), rows[2].id: (work.id, 1)},
        )
        await session.commit()

        assert stats.merged == 0
        seasons = {}
        for row in rows:
            await session.refresh(row)
            seasons[row.title] = row.season
        assert seasons == {"大主宰 第1季": 2, "大主宰 第2季": 3, "大主宰 第3季": 1}

    @pytest.mark.asyncio
    async def test_old_home_is_reported_as_touched(self, session) -> None:
        """搬离的旧 Work 也要进 `touched_works`，否则它的计数停在旧值上。"""
        old = await self._work(session, "旧作品")
        new = await self._work(session, "新作品")
        row = _media("大主宰", work_id=old.id, season=1)
        session.add(row)
        await session.commit()

        stats = await assign_identities(session, {row.id: (new.id, 1)})
        await session.commit()

        assert stats.touched_works == {old.id, new.id}

    @pytest.mark.asyncio
    async def test_rows_already_in_place_are_not_written(self, session) -> None:
        """重跑时已经坐对位置的行一次 UPDATE 都不该发 —— 这是幂等的来源。"""
        work = await self._work(session, "大主宰")
        row = _media("大主宰 第1季", work_id=work.id, season=1)
        session.add(row)
        await session.commit()

        stats = await assign_identities(session, {row.id: (work.id, 1)})
        assert (stats.moved, stats.merged, stats.parked, stats.conflicts) == (0, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_extra_titles_refresh_only_the_survivor(self, session) -> None:
        work = await self._work(session, "大主宰")
        rows = [_media("国漫《大主宰》更至03"), _media("大主宰 4K高码")]
        resources = [_resource(i) for i in range(2)]
        session.add_all([*rows, *resources])
        await session.commit()
        await _attach(session, [(r, [res]) for r, res in zip(rows, resources, strict=True)])

        await assign_identities(
            session,
            {rows[0].id: (work.id, NO_SEASON), rows[1].id: (work.id, NO_SEASON)},
            extra_titles={rows[0].id: "大主宰", rows[1].id: "大主宰"},
        )
        await session.commit()

        survivor = await session.scalar(select(Media).where(Media.work_id == work.id))
        assert survivor.title == "大主宰"


class TestSettleKnownWorks:
    """知识沉淀：买过一次的作品身份要能免费复用，但只认字面相等。

    这一步没有模型兜底，判错一次就是一次误并（不可逆）。所以这些断言分两半：
    该沉淀的必须沉淀下来（否则白花钱重复问），不该沉淀的必须一个都不碰。
    """

    async def _settle(self, session, *, apply: bool = True):
        from funflix.services.canon.sediment import settle_known_works

        report, keys = await settle_known_works(session, apply=apply)
        return report, keys

    @pytest.mark.asyncio
    async def test_bare_work_name_inherits_the_decided_identity(self, session) -> None:
        """模型判出「`大主宰2` 属于《大主宰》」时，也就确立了 `大主宰` 是个规范作品名。

        于是后来冒出的光杆 `大主宰` 键不必再问模型 —— 答案已经在库里。
        """
        session.add(_canon("大主宰2", work_title="大主宰", season=2, media_type=MediaType.ANIME))
        session.add(
            TitleCanon(norm_key="大主宰", status=CanonState.PENDING, work_title="大主宰 第3季")
        )
        await session.commit()

        report, keys = await self._settle(session)

        assert report.settled == 1
        assert keys == {"大主宰"}
        row = await session.get(TitleCanon, "大主宰")
        assert row.status == CanonState.DECIDED
        assert row.work_title == "大主宰"
        assert row.work_norm_key == "大主宰"
        assert row.media_type is MediaType.ANIME, "类型是作品级属性，要继承"
        assert row.model == "sediment:known-work"
        assert row.confidence is None, "不是模型给的置信度，不能编一个"

    @pytest.mark.asyncio
    async def test_season_and_year_are_not_inherited(self, session) -> None:
        """季和年都是**季级**属性，继承兄弟行的值就是错的。

        `season` 这一列的语义是「这个键锁定了哪一季」，光杆作品名没锁定任何一季；
        继承了就等于把整部剧钉死在第 2 季上（理由同 `lookup.pending_row`）。
        年份同理 —— 第 1 季 2020、第 3 季 2023，拿兄弟行的年份填进来必然错。
        """
        session.add(_canon("大主宰2", work_title="大主宰", season=2, year=2022))
        session.add(TitleCanon(norm_key="大主宰", status=CanonState.PENDING))
        await session.commit()

        await self._settle(session)

        row = await session.get(TitleCanon, "大主宰")
        assert row.season is None
        assert row.year == UNKNOWN_YEAR

    @pytest.mark.asyncio
    async def test_similar_but_different_keys_are_left_alone(self, session) -> None:
        """只认字面相等。前缀/包含关系的区分要读语义，那是花钱请模型的理由。"""
        session.add(_canon("大主宰2", work_title="大主宰"))
        for key in ("天命大主宰", "大主宰动态漫", "从大主宰开始打卡"):
            session.add(TitleCanon(norm_key=key, status=CanonState.PENDING))
        await session.commit()

        report, keys = await self._settle(session)

        assert report.settled == 0
        assert keys == set()
        for key in ("天命大主宰", "大主宰动态漫", "从大主宰开始打卡"):
            row = await session.get(TitleCanon, key)
            assert row.status == CanonState.PENDING

    @pytest.mark.asyncio
    async def test_junk_decisions_are_not_a_source_of_truth(self, session) -> None:
        """判成垃圾的裁决没有作品身份可继承，不能拿它去沉淀。"""
        session.add(_canon("大主宰", work_title=None, work_norm_key=None, is_junk=True))
        session.add(TitleCanon(norm_key="斗破苍穹", status=CanonState.PENDING))
        await session.commit()

        report, _keys = await self._settle(session)

        assert report.known_works == 0
        assert report.settled == 0

    @pytest.mark.asyncio
    async def test_already_decided_rows_are_not_rewritten(self, session) -> None:
        """已裁决的行是花钱买来的，沉淀不能覆盖它 —— 那会把季号抹掉。"""
        session.add(_canon("大主宰", work_title="大主宰"))
        session.add(_canon("大主宰2", work_title="大主宰", season=2))
        await session.commit()

        report, _keys = await self._settle(session)

        assert report.settled == 0
        row = await session.get(TitleCanon, "大主宰2")
        assert row.season == 2
        assert row.model != "sediment:known-work"

    @pytest.mark.asyncio
    async def test_dry_run_counts_without_writing(self, session) -> None:
        session.add(_canon("大主宰2", work_title="大主宰"))
        session.add(TitleCanon(norm_key="大主宰", status=CanonState.PENDING))
        await session.commit()

        report, keys = await self._settle(session, apply=False)

        assert report.matched == 1
        assert report.settled == 0
        assert keys == {"大主宰"}, "dry-run 也要把命中的键报出来，否则待送块数虚高"
        row = await session.get(TitleCanon, "大主宰")
        assert row.status == CanonState.PENDING

    @pytest.mark.asyncio
    async def test_work_title_pick_is_deterministic(self, session) -> None:
        """同一个作品键底下挂着几种写法时，取哪个必须是确定的。

        不确定的话同一批数据重跑会写出不同的 `work_title`，搜索结果跟着抖。
        """
        # 三行同属一个作品键，但 `work_title` 有两种写法（不同字面洗出同一个键）
        session.add(_canon("大主宰2", work_title="大主宰", work_norm_key="大主宰"))
        session.add(_canon("大主宰3", work_title="大主宰", work_norm_key="大主宰"))
        session.add(_canon("大主宰ii", work_title="大主宰 年番", work_norm_key="大主宰"))
        session.add(TitleCanon(norm_key="大主宰", status=CanonState.PENDING))
        await session.commit()

        await self._settle(session)

        row = await session.get(TitleCanon, "大主宰")
        assert row.work_title == "大主宰", "取出现次数最多的写法"
