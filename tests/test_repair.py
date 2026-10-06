"""持续修复机制：检测、应用、重排队。

这套机制会**不可逆地删改几十万行生产数据**，而且是挂在流水线上自动跑的 ——
没有人在每一轮前面核对。所以断言针对的都是「跑错了会丢数据」的那种失败：

- 规则没变时必须**零写入**（否则这个节点不能天天跑）
- 类型绝不能被降级成 unknown（一轮就能把全库的类型刷平）
- 删除不能碰 resource 行（链接是真实采集成本）
- 多合一不能丢资源关联
- 诊断结论变了，旧任务必须撤掉（否则 delete 和 retitle 会同时在队列里）
- 爆炸半径闸门必须真的拦得住
"""

from __future__ import annotations

import itertools
import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from funflix.base.enums import MediaType, ParseStatus, Provider, Quality, SourceType
from funflix.models import Media, RawDocument, Resource, Work, media_resource, utcnow
from funflix.models.canon import CanonState, TitleCanon
from funflix.models.media import NO_SEASON, UNKNOWN_YEAR
from funflix.models.raw import PARSE_RULES_VERSION
from funflix.models.repair import RepairKind, RepairState, RepairSymptom, RepairTask
from funflix.services.counters import refresh_media_counters
from funflix.services.repair import (
    BlastRadiusExceeded,
    MediaFacts,
    apply_repairs,
    plan_key,
    plan_repair,
    requeue_stale_documents,
    scan_media,
)
from funflix.services.text.normalize import extract_season, series_norm_key

_SEQ = itertools.count()


#: 近似「当年的归一」：去掉标点和空白、转小写，但**不剥**任何噪声词。
_SQUASH = re.compile(r"[\s\W_]+", re.UNICODE)


def _stale_key(title: str) -> str:
    """模拟**旧规则**给这一行算出的作品键。

    这是复现生产现状的关键一步，而且只能手工构造 —— 现在的每一个键函数
    （`norm_key` / `series_norm_key` / `block_key`）内部都会先跑 `clean_title`，
    所以拿它们去算脏标题会直接得到**干净**的键：
    `series_norm_key('白月光向我自荐枕席 作者:烟叶')` 就等于
    `series_norm_key('白月光向我自荐枕席')`。用它们造夹具的话作品键天生就是
    对的，`rehome` 这条路一个用例都测不到。

    生产库里的 `work.norm_key` 是**当年**算完存下来的字符串，那时 `作者`
    还不在 `_SCRAPE_CUT_RE` 里，键里就带着作者名。所以这里按「没剥掉任何
    噪声词」的样子直接压一个键出来。
    """
    assert _SQUASH.sub("", title).lower() != series_norm_key(title), (
        f"{title!r} 的旧键和新键算出来一样，这个夹具测不到 rehome"
    )
    return _SQUASH.sub("", title).lower()


def _facts(title: str, **kw) -> MediaFacts:
    """一行 media 的当前值。默认是「按现在的规则已经挂对了」的状态。

    `work_norm_key` 默认取 `series_norm_key(title)`，于是 `plan_repair` 返回
    非 `None` 就一定意味着**规则判出了真实的漂移**，而不是夹具自己造出来的
    不一致。要测 `rehome` 就显式传 `_stale_key(title)`。
    """
    kw.setdefault("media_type", MediaType.UNKNOWN)
    kw.setdefault("year", UNKNOWN_YEAR)
    kw.setdefault("season", NO_SEASON)
    kw.setdefault("resource_count", 3)
    kw.setdefault("work_norm_key", series_norm_key(title))
    return MediaFacts(title=title, **kw)


def _resource(n: int) -> Resource:
    now = utcnow()
    return Resource(
        provider=Provider.QUARK,
        share_id=f"r{n:06d}",
        url=f"https://pan.quark.cn/s/r{n:06d}",
        quality=Quality.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
    )


async def _media_row(session, title: str, *, resources: int = 1, **kw) -> Media:
    """落一行 media 到库里，挂上资源并刷好季级计数。

    必须真的挂资源：`plan_repair` 把 `resource_count == 0` 判成空壳要删，
    所以一个不挂资源的夹具会让每个测试都走到删除分支上去。
    """
    kw.setdefault("media_type", MediaType.UNKNOWN)
    kw.setdefault("year", UNKNOWN_YEAR)
    kw.setdefault("season", NO_SEASON)
    kw.setdefault("norm_key", title)
    kw.setdefault("aliases", [])
    if "work" not in kw and "work_id" not in kw:
        kw["work"] = Work(
            title=title,
            norm_key=kw.pop("work_norm_key", series_norm_key(title)),
            aliases=[],
            media_type=kw["media_type"],
            year=kw["year"],
        )
    media = Media(title=title, **kw)
    session.add(media)
    await session.flush()
    for _ in range(resources):
        res = _resource(next(_SEQ))
        session.add(res)
        await session.flush()
        await session.execute(
            media_resource.insert().values(
                media_id=media.id, resource_id=res.id, created_at=utcnow()
            )
        )
    await session.commit()
    if resources:
        await refresh_media_counters(session, [media.id])
        await session.commit()
    await session.refresh(media)
    return media


class TestPlanNoOp:
    """规则没变 → 一个任务都不建。这是整套机制能天天跑的前提。"""

    @pytest.mark.parametrize(
        ("title", "media_type"),
        [
            ("全民攻防:我有签到系统", MediaType.UNKNOWN),
            ("白月光向我自荐枕席", MediaType.UNKNOWN),
            ("流浪地球", MediaType.MOVIE),
            # 季号已经判出来、类型也已经是 tv 的行 —— 全都对上了。
            ("大主宰 第2季", MediaType.TV),
        ],
    )
    def test_already_clean_rows_plan_nothing(self, title, media_type):
        # 季号也得对上，否则会判成 key_drift。
        facts = _facts(title, season=extract_season(title) or NO_SEASON, media_type=media_type)
        assert plan_repair(facts, None) is None

    def test_matching_pending_canon_still_plans_nothing(self):
        """库里有一行 `pending` 裁决不该让这一行动起来。

        `pending` 的语义是「还没裁决」，`resolve_target` 对它回落到规则 ——
        所以结论必须和没有裁决行时一模一样。写错的话每一轮 scan 都会把
        全库 89 万行刷成任务，因为生产库里**每个键**都有一行 pending。
        """
        canon = TitleCanon(
            norm_key=series_norm_key("流浪地球"),
            work_norm_key=series_norm_key("流浪地球"),
            work_title="流浪地球",
            season=None,
            media_type=MediaType.UNKNOWN,
            year=UNKNOWN_YEAR,
            is_junk=False,
            status=CanonState.PENDING,
        )
        assert plan_repair(_facts("流浪地球"), canon) is None


class TestPlanTypeAndYear:
    """类型只补不改。写成双向的，一轮 scan 就能把全库的类型刷成 unknown。"""

    def test_type_is_never_downgraded_to_unknown(self):
        """正文判出的 movie 不能被「只看标题得到 unknown」推翻。

        生产库里绝大多数行的 `media_type` 来自正文的 `类型:电影`，而标题
        `流浪地球` 本身给不出任何信号。这条反了就是一次全库降级。
        """
        assert plan_repair(_facts("流浪地球", media_type=MediaType.MOVIE), None) is None

    def test_type_filled_when_unknown(self):
        plan = plan_repair(_facts("大主宰 第2季", season=2, media_type=MediaType.UNKNOWN), None)
        assert plan is not None
        assert plan.kind == RepairKind.RETITLE
        assert plan.payload["media_type"] == MediaType.TV.value

    def test_book_signal_overrides_existing_video_type(self):
        """`作者:` 是决定性信号，能推翻已有的影视类型。

        这是整套机制的第一个用例：4,399 行小说分享现在挂在影视类型上
        污染搜索。
        """
        facts = _facts(
            "全民攻防:我有签到系统 作者:奏光 txt",
            media_type=MediaType.ANIME,
            work_norm_key=series_norm_key("全民攻防:我有签到系统 作者:奏光 txt"),
        )
        plan = plan_repair(facts, None)
        assert plan is not None
        assert plan.payload["media_type"] == MediaType.BOOK.value

    def test_type_detected_from_stored_title_not_cleaned(self):
        """判据要从**库里存着的**标题上取，不是洗完的那个。

        `clean_title` 会把 `作者:奏光 txt` 整段剥掉 —— 在洗后的标题上判，
        书刊信号已经不存在了，这个用例永远不会触发。同理 `(2019)` 洗完也
        没了，年份一样取不到。
        """
        facts = _facts("流浪地球 (2019)", work_norm_key=series_norm_key("流浪地球 (2019)"))
        plan = plan_repair(facts, None)
        assert plan is not None
        assert plan.payload["year"] == 2019
        assert plan.payload["title"] == "流浪地球"

    def test_year_is_never_overwritten(self):
        """已有年份不被标题的推断覆盖 —— 抽取器看到的信息比标题多。"""
        facts = _facts(
            "流浪地球 (2019)",
            year=2021,
            work_norm_key=series_norm_key("流浪地球 (2019)"),
        )
        plan = plan_repair(facts, None)
        assert plan is not None
        assert plan.payload["year"] == 2021


class TestPlanDelete:
    """删除判得错一条就少一部作品，判漏一条就多一行垃圾。"""

    def test_junk_beats_everything(self):
        """垃圾行先摘出去，不能让它参与后面的重挂和多合一。

        一旦并进真作品，那条垃圾链接就永久挂在作品下面了。
        """
        plan = plan_repair(_facts("夸克", work_norm_key="xx"), None)
        assert plan is not None
        assert (plan.kind, plan.symptom) == (RepairKind.DELETE, RepairSymptom.JUNK)

    def test_empty_shell_deleted(self):
        plan = plan_repair(_facts("流浪地球", resource_count=0), None)
        assert plan is not None
        assert (plan.kind, plan.symptom) == (RepairKind.DELETE, RepairSymptom.EMPTY_SHELL)

    def test_junk_decision_deletes(self):
        """LLM 裁决说不是作品，走和规则判出的 junk 完全一样的删除语义。"""
        canon = TitleCanon(
            norm_key=series_norm_key("流浪地球"),
            work_norm_key="",
            work_title="",
            season=None,
            media_type=MediaType.UNKNOWN,
            year=UNKNOWN_YEAR,
            is_junk=True,
            status=CanonState.DECIDED,
        )
        plan = plan_repair(_facts("流浪地球"), canon)
        assert plan is not None
        assert (plan.kind, plan.symptom) == (RepairKind.DELETE, RepairSymptom.JUNK)


class TestPlanRehome:
    """重挂的 payload 要同时带上新标题 —— 否则「改完标题下一轮才搬家」。"""

    def test_stale_work_key_drives_rehome_carrying_new_title(self):
        """用户给的那个例子：`白月光向我自荐枕席 作者:烟叶`，且作品键是旧的。

        这是生产现状：`work.norm_key` 是当年旧规则算完存下来的，带着作者名。
        标题和作品键都要改 —— 而这**一个** rehome 任务就同时做完两件事，
        不是先 retitle 再等下一轮搬家。`assign_identities` 的 `extra_titles`
        支持在搬迁的同一步刷标题。
        """
        dirty = "白月光向我自荐枕席 作者:烟叶"
        plan = plan_repair(_facts(dirty, work_norm_key=_stale_key(dirty)), None)
        assert plan is not None
        assert (plan.kind, plan.symptom) == (RepairKind.REHOME, RepairSymptom.KEY_DRIFT)
        assert plan.payload["title"] == "白月光向我自荐枕席"
        assert plan.payload["work_norm_key"] == series_norm_key("白月光向我自荐枕席")
        assert plan.payload["media_type"] == MediaType.BOOK.value

    def test_author_label_alone_is_only_a_retitle(self):
        """作品键没漂移时，同一个脏标题只需要 retitle。

        `series_norm_key` 内部会先跑 `clean_title`，所以**今天**算出来的键
        对脏标题和干净标题是同一个 —— 作者名早就不在键里了。要改的只有
        用户看得见的 `title` 和被污染的 `media_type`。
        """
        dirty = "白月光向我自荐枕席 作者:烟叶"
        assert series_norm_key(dirty) == series_norm_key("白月光向我自荐枕席")
        plan = plan_repair(_facts(dirty), None)
        assert plan is not None
        assert (plan.kind, plan.symptom) == (RepairKind.RETITLE, RepairSymptom.TITLE_DRIFT)
        assert plan.payload["title"] == "白月光向我自荐枕席"
        assert plan.payload["media_type"] == MediaType.BOOK.value

    def test_unassigned_row_is_rehomed(self):
        """`work_norm_key is None`（迁移 B 之前的遗留）必须被检出。

        生产库里 89 万行都是这个状态，漏掉它们等于这套机制对现状完全无效。
        """
        plan = plan_repair(_facts("流浪地球", work_norm_key=None), None)
        assert plan is not None
        assert plan.kind == RepairKind.REHOME

    def test_decided_canon_locks_season(self):
        canon = TitleCanon(
            norm_key=series_norm_key("大主宰"),
            work_norm_key=series_norm_key("大主宰"),
            work_title="大主宰",
            season=2,
            media_type=MediaType.ANIME,
            year=UNKNOWN_YEAR,
            is_junk=False,
            status=CanonState.DECIDED,
        )
        plan = plan_repair(_facts("大主宰", season=NO_SEASON), canon)
        assert plan is not None
        assert plan.kind == RepairKind.REHOME
        assert plan.payload["season"] == 2
        assert plan.payload["media_type"] == MediaType.ANIME.value

    def test_row_already_matching_decision_plans_nothing(self):
        """裁决已经落实的行不能再被检出 —— 否则 scan 和裁决会无限对打。"""
        canon = TitleCanon(
            norm_key=series_norm_key("大主宰"),
            work_norm_key=series_norm_key("大主宰"),
            work_title="大主宰",
            season=2,
            media_type=MediaType.ANIME,
            year=UNKNOWN_YEAR,
            is_junk=False,
            status=CanonState.DECIDED,
        )
        facts = _facts(
            "大主宰",
            season=2,
            media_type=MediaType.ANIME,
            work_norm_key=series_norm_key("大主宰"),
        )
        assert plan_repair(facts, canon) is None


class TestTaskTable:
    """部分唯一索引是幂等的支点。"""

    @pytest.mark.asyncio
    async def test_pending_is_unique_per_kind_and_media(self, session):
        media = await _media_row(session, "流浪地球")
        now = utcnow()
        for _ in range(2):
            session.add(
                RepairTask(
                    kind=RepairKind.RETITLE,
                    symptom=RepairSymptom.TITLE_DRIFT,
                    media_id=media.id,
                    payload={},
                    status=RepairState.PENDING,
                    detected_at=now,
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_history_does_not_block_new_task(self, session):
        """已处理的历史任务要留着当审计记录，不能挡住下一轮的新任务。

        同一行修完之后再次被检出是**预期行为** —— 说明还有残留问题。
        索引做成全表唯一就会把它挡掉。
        """
        media = await _media_row(session, "流浪地球")
        now = utcnow()
        session.add(
            RepairTask(
                kind=RepairKind.RETITLE,
                symptom=RepairSymptom.TITLE_DRIFT,
                media_id=media.id,
                payload={},
                status=RepairState.APPLIED,
                detected_at=now,
                applied_at=now,
            )
        )
        await session.commit()
        session.add(
            RepairTask(
                kind=RepairKind.RETITLE,
                symptom=RepairSymptom.TITLE_DRIFT,
                media_id=media.id,
                payload={},
                status=RepairState.PENDING,
                detected_at=now,
            )
        )
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(RepairTask)) == 2


class TestScan:
    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, session):
        await _media_row(session, "白月光向我自荐枕席 作者:烟叶")
        report = await scan_media(session)
        assert report.retitle == 1
        assert report.created == 0
        assert await session.scalar(select(func.count()).select_from(RepairTask)) == 0

    @pytest.mark.asyncio
    async def test_clean_library_creates_no_task(self, session):
        """空转验收：规则没变 → 扫完一个任务都不建、一个字都不写。"""
        await _media_row(session, "流浪地球", media_type=MediaType.MOVIE)
        report = await scan_media(session, dry_run=False)
        assert report.scanned == 1
        assert report.planned == 0
        assert report.created == 0
        assert await session.scalar(select(func.count()).select_from(RepairTask)) == 0

    @pytest.mark.asyncio
    async def test_is_idempotent(self, session):
        """连扫两轮，第二轮不该再建任务。"""
        await _media_row(session, "白月光向我自荐枕席 作者:烟叶")
        first = await scan_media(session, dry_run=False)
        second = await scan_media(session, dry_run=False)
        assert first.created == 1
        assert (second.created, second.unchanged, second.cancelled) == (0, 1, 0)
        assert await session.scalar(select(func.count()).select_from(RepairTask)) == 1

    @pytest.mark.asyncio
    async def test_cancels_task_when_diagnosis_changes(self, session):
        """结论从 retitle 变成 delete，旧的 retitle 必须撤掉。

        不撤的话两个任务同时在队列里，`apply` 先跑 delete 再跑 retitle ——
        第二步去改一行已经不存在的 media。索引是按 `(kind, media_id)` 建的，
        挡不住这种跨 kind 并存。
        """
        media = await _media_row(session, "流浪地球")
        session.add(
            RepairTask(
                kind=RepairKind.DELETE,
                symptom=RepairSymptom.JUNK,
                media_id=media.id,
                payload={},
                status=RepairState.PENDING,
                detected_at=utcnow(),
            )
        )
        await session.commit()

        report = await scan_media(session, dry_run=False)
        assert report.cancelled == 1
        stale = await session.scalar(
            select(RepairTask.status).where(RepairTask.kind == RepairKind.DELETE)
        )
        assert stale == RepairState.SKIPPED

    @pytest.mark.asyncio
    async def test_cancels_task_when_no_longer_needed(self, session):
        """parse 顺手改对了的行，旧任务要撤 —— 否则队列里永久积压空任务。"""
        media = await _media_row(session, "流浪地球", media_type=MediaType.MOVIE)
        session.add(
            RepairTask(
                kind=RepairKind.RETITLE,
                symptom=RepairSymptom.TITLE_DRIFT,
                media_id=media.id,
                payload={"title": "流浪地球"},
                status=RepairState.PENDING,
                detected_at=utcnow(),
            )
        )
        await session.commit()

        report = await scan_media(session, dry_run=False)
        assert (report.planned, report.cancelled) == (0, 1)

    @pytest.mark.asyncio
    async def test_refreshes_payload_when_conclusion_changes(self, session):
        dirty = "白月光向我自荐枕席 作者:烟叶"
        media = await _media_row(session, dirty, work_norm_key=_stale_key(dirty))
        session.add(
            RepairTask(
                kind=RepairKind.REHOME,
                symptom=RepairSymptom.KEY_DRIFT,
                media_id=media.id,
                payload={"title": "过时的值"},
                status=RepairState.PENDING,
                detected_at=utcnow(),
            )
        )
        await session.commit()

        report = await scan_media(session, dry_run=False)
        assert (report.created, report.refreshed) == (0, 1)
        task = await session.scalar(select(RepairTask))
        assert task is not None
        assert task.payload["title"] == "白月光向我自荐枕席"

    @pytest.mark.asyncio
    async def test_forecasts_merge(self, session):
        """要搬进去的那个身份已经有人坐着 → dry-run 要提前报出这次多合一。"""
        clean = "白月光向我自荐枕席"
        dirty = f"{clean} 作者:烟叶"
        await _media_row(session, clean)
        await _media_row(session, dirty, work_norm_key=_stale_key(dirty))
        report = await scan_media(session)
        assert report.rehome == 1
        assert report.expect_merge == 1

    @pytest.mark.asyncio
    async def test_limit_caps_rows_scanned(self, session):
        for n in range(3):
            await _media_row(session, f"白月光向我自荐枕席{n} 作者:烟叶")
        report = await scan_media(session, limit=2)
        assert report.scanned == 2


class TestApply:
    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, session):
        await _media_row(session, "白月光向我自荐枕席 作者:烟叶")
        await scan_media(session, dry_run=False)
        report = await apply_repairs(session)
        assert report.retitle == 1
        assert report.retitled == 0
        media = await session.scalar(select(Media))
        assert media is not None
        assert media.title == "白月光向我自荐枕席 作者:烟叶"

    @pytest.mark.asyncio
    async def test_delete_keeps_resource_rows(self, session):
        """删 media 不能碰 resource —— 链接是真实的采集成本，只是归属错了。"""
        media = await _media_row(session, "夸克", resources=2)
        session.add(
            RepairTask(
                kind=RepairKind.DELETE,
                symptom=RepairSymptom.JUNK,
                media_id=media.id,
                payload={},
                status=RepairState.PENDING,
                detected_at=utcnow(),
            )
        )
        await session.commit()

        report = await apply_repairs(session, dry_run=False, force=True)
        assert report.deleted == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 0
        assert await session.scalar(select(func.count()).select_from(Resource)) == 2

    @pytest.mark.asyncio
    async def test_retitle_updates_norm_key(self, session):
        """`norm_key` 是逐标题身份键，标题变了它就必须跟着变。

        不刷的话这一行在搜索和去重两条路上都还顶着旧标题的键。
        """
        media = await _media_row(session, "大主宰 第2季", season=2, media_type=MediaType.UNKNOWN)
        await scan_media(session, dry_run=False)
        report = await apply_repairs(session, dry_run=False)
        assert report.retitled == 1

        await session.refresh(media)
        assert media.media_type is MediaType.TV
        assert media.norm_key == "大主宰第2季"

    @pytest.mark.asyncio
    async def test_rehome_merges_and_keeps_links(self, session):
        """多合一不能丢资源关联 —— 败者的链接要迁到存活行上。"""
        clean = "白月光向我自荐枕席"
        dirty = f"{clean} 作者:烟叶"
        keeper = await _media_row(session, clean, resources=2)
        loser = await _media_row(session, dirty, resources=3, work_norm_key=_stale_key(dirty))
        await scan_media(session, dry_run=False)
        report = await apply_repairs(session, dry_run=False, force=True)

        assert report.merged == 1
        assert await session.scalar(select(func.count()).select_from(Media)) == 1
        survivor = await session.scalar(select(Media))
        assert survivor is not None
        links = await session.scalar(
            select(func.count())
            .select_from(media_resource)
            .where(media_resource.c.media_id == survivor.id)
        )
        assert links == 5
        assert await session.scalar(select(func.count()).select_from(Resource)) == 5
        assert survivor.id in {keeper.id, loser.id}

    @pytest.mark.asyncio
    async def test_skips_task_whose_media_is_gone(self, session):
        """任务指向的 media 已经不在了（上一轮作为败者被并掉）→ skipped，不报错。"""
        media = await _media_row(session, "流浪地球")
        media_id = media.id
        session.add(
            RepairTask(
                kind=RepairKind.RETITLE,
                symptom=RepairSymptom.TITLE_DRIFT,
                media_id=media_id,
                payload={"title": "流浪地球"},
                status=RepairState.PENDING,
                detected_at=utcnow(),
            )
        )
        await session.commit()
        await session.delete(media)
        await session.commit()

        report = await apply_repairs(session, dry_run=False)
        assert (report.skipped, report.retitled) == (1, 0)
        task = await session.scalar(select(RepairTask))
        assert task is not None
        assert task.status == RepairState.SKIPPED

    @pytest.mark.asyncio
    async def test_blast_radius_gate_refuses(self, session):
        """要删的行超过全库 5% 就拒绝 —— 更可能是规则写错了，不是数据坏了。"""
        media = await _media_row(session, "流浪地球")
        session.add(
            RepairTask(
                kind=RepairKind.DELETE,
                symptom=RepairSymptom.JUNK,
                media_id=media.id,
                payload={},
                status=RepairState.PENDING,
                detected_at=utcnow(),
            )
        )
        await session.commit()

        with pytest.raises(BlastRadiusExceeded):
            await apply_repairs(session, dry_run=False)
        assert await session.scalar(select(func.count()).select_from(Media)) == 1

    @pytest.mark.asyncio
    async def test_gate_fires_in_dry_run_too(self, session):
        """dry-run 的用处就是让人**提前**看到这个拦截，所以它也要抛。"""
        media = await _media_row(session, "流浪地球")
        session.add(
            RepairTask(
                kind=RepairKind.DELETE,
                symptom=RepairSymptom.JUNK,
                media_id=media.id,
                payload={},
                status=RepairState.PENDING,
                detected_at=utcnow(),
            )
        )
        await session.commit()
        with pytest.raises(BlastRadiusExceeded):
            await apply_repairs(session)

    @pytest.mark.asyncio
    async def test_force_passes_the_gate(self, session):
        media = await _media_row(session, "夸克")
        session.add(
            RepairTask(
                kind=RepairKind.DELETE,
                symptom=RepairSymptom.JUNK,
                media_id=media.id,
                payload={},
                status=RepairState.PENDING,
                detected_at=utcnow(),
            )
        )
        await session.commit()
        report = await apply_repairs(session, dry_run=False, force=True)
        assert report.deleted == 1


class TestApplyKeyScope:
    """`--key` 的单组演练：`scan --key X` 建的任务，`apply --key X` 必须正好领到。

    两边用的是同一个 `plan_key`。不是同一个的话演练会「扫出 1 个任务、应用 0 个」
    —— 而这正是上生产前唯一的小步试探手段，失准等于没有。
    """

    @pytest.mark.asyncio
    async def test_key_only_applies_its_own_group(self, session):
        dirty = "白月光向我自荐枕席 作者:烟叶"
        target = await _media_row(session, dirty)
        other = await _media_row(session, "全民攻防:我有签到系统 作者:奏光")
        await scan_media(session, dry_run=False)
        assert await session.scalar(select(func.count()).select_from(RepairTask)) == 2

        report = await apply_repairs(session, dry_run=False, key=plan_key(dirty), force=True)
        assert report.retitled == 1

        await session.refresh(target)
        await session.refresh(other)
        assert target.title == "白月光向我自荐枕席"
        # 另一组一个字都没动，它的任务还在队列里等下一轮。
        assert other.title == "全民攻防:我有签到系统 作者:奏光"
        pending = await session.scalar(
            select(func.count())
            .select_from(RepairTask)
            .where(RepairTask.status == RepairState.PENDING)
        )
        assert pending == 1

    @pytest.mark.asyncio
    async def test_unknown_key_is_a_no_op(self, session):
        """键一行都没匹配上时必须什么都不做 —— 不能退化成「全领」。

        `media_ids` 的空集合和 `None` 在 `_claim` 里语义相反，敲错一个键
        就把全库刷一遍是不可接受的。
        """
        await _media_row(session, "白月光向我自荐枕席 作者:烟叶")
        await scan_media(session, dry_run=False)

        report = await apply_repairs(session, dry_run=False, key="这个键不存在", force=True)
        assert (report.retitle, report.retitled, report.deleted) == (0, 0, 0)
        pending = await session.scalar(
            select(func.count())
            .select_from(RepairTask)
            .where(RepairTask.status == RepairState.PENDING)
        )
        assert pending == 1


def _document(n: int, *, status: ParseStatus, version: str | None, attempts: int = 0):
    now = utcnow()
    return RawDocument(
        content=f"文档 {n}",
        content_hash=f"{n:064d}",
        source_type=SourceType.UNKNOWN,
        collected_at=now,
        extra={},
        parse_status=status,
        parse_attempts=attempts,
        parse_rules_version=version,
        next_parse_at=now + timedelta(hours=6),
        parse_error="上次失败了" if attempts else None,
    )


class TestRequeue:
    @pytest.mark.asyncio
    async def test_only_stale_and_done_are_requeued(self, session):
        """只收 `done` 且版本对不上的。

        `pending` / `failed` 本来就在队列里（或在退避里），碰它们只会把
        `parse_attempts` 清掉，让一份一直失败的文档重新开始无意义的重试。
        """
        session.add_all(
            [
                _document(1, status=ParseStatus.DONE, version=None),
                _document(2, status=ParseStatus.DONE, version="rules-v0"),
                _document(3, status=ParseStatus.DONE, version=PARSE_RULES_VERSION),
                _document(4, status=ParseStatus.FAILED, version=None, attempts=3),
            ]
        )
        await session.commit()

        report = await requeue_stale_documents(session, dry_run=False)
        assert (report.stale, report.requeued) == (2, 2)

        pending = await session.scalar(
            select(func.count())
            .select_from(RawDocument)
            .where(RawDocument.parse_status == ParseStatus.PENDING)
        )
        assert pending == 2
        failed = await session.scalar(
            select(RawDocument).where(RawDocument.parse_status == ParseStatus.FAILED)
        )
        assert failed is not None
        assert failed.parse_attempts == 3

    @pytest.mark.asyncio
    async def test_requeue_clears_backoff(self, session):
        """清掉退避和 attempts —— 这次重排和上次的失败没有关系。

        不清的话一份历史上失败过几次的文档会带着旧的 `next_parse_at`
        重新进队列，可能几小时内都捞不起来。
        """
        session.add(_document(1, status=ParseStatus.DONE, version="rules-v0", attempts=2))
        await session.commit()
        await requeue_stale_documents(session, dry_run=False)

        doc = await session.scalar(select(RawDocument))
        assert doc is not None
        assert doc.parse_status is ParseStatus.PENDING
        assert (doc.parse_attempts, doc.next_parse_at, doc.lease_until) == (0, None, None)
        assert doc.parse_error is None

    @pytest.mark.asyncio
    async def test_dry_run_reports_without_writing(self, session):
        session.add_all([_document(n, status=ParseStatus.DONE, version=None) for n in range(1, 4)])
        await session.commit()

        report = await requeue_stale_documents(session, limit=2)
        assert (report.stale, report.requeued) == (3, 2)
        done = await session.scalar(
            select(func.count())
            .select_from(RawDocument)
            .where(RawDocument.parse_status == ParseStatus.DONE)
        )
        assert done == 3

    @pytest.mark.asyncio
    async def test_limit_throttles(self, session):
        """限额是节流阀：一轮放一批，parse 节点按自己的速度消化。"""
        session.add_all([_document(n, status=ParseStatus.DONE, version=None) for n in range(1, 6)])
        await session.commit()

        report = await requeue_stale_documents(session, dry_run=False, limit=2)
        assert report.requeued == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RawDocument)
                .where(RawDocument.parse_status == ParseStatus.DONE)
            )
            == 3
        )
