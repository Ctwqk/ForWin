from __future__ import annotations

import json
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sqlalchemy import select

from forwin.application.read_models import build_project_detail
from forwin.book_state import BookStateRepository
from forwin.canon_names import CanonNameAnchor, find_canon_name_violations
from forwin.checker.rules import ContinuityChecker
from forwin.context.assembler_core import assemble_context
from forwin.director.arc_director import ArcDirector
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
from forwin.map.models import MapRegionRow
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.models.subworld import (
    SubWorld,
    SubWorldRosterItem,
    project_scoped_subworld_id,
)
from forwin.planning.arc_envelope import ArcEnvelopeManager, ArcStructureDraftData
from forwin.planning.stage_analysis import ReplanGovernor, StageAssessment
from forwin.generation.pipeline import ChapterPipeline
from forwin.protocol import (
    ArcPayoffMap,
    ChapterEntryTarget,
    ChapterExperiencePlan,
    ReaderPromise,
    ReviewVerdict,
    SubWorldPlanDelta,
    SubWorldSummary,
    WriterOutput,
)
from forwin.protocol.book_state import WorldNode
from forwin.protocol.review import ContinuityIssue
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.subworld_manager import SubWorldManager
from tests.chapter_delta_helpers import chapter_delta
from tests.postgres import postgres_test_url
from forwin.writer.prompt_core import (
    _canon_name_anchor_section,
    build_single_chapter_draft_prompt,
)


class _BookStateQueryStub:
    def __init__(self, recent_beats: list[str]) -> None:
        self.recent_beats = recent_beats
        self.thread_reads: list[tuple[str, int]] = []

    def active_threads(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
    ) -> list[object]:
        self.thread_reads.append((project_id, as_of_chapter))
        return [SimpleNamespace(description="", recent_beats=self.recent_beats)]

    def canon_name_anchors(self, project_id: str, *, as_of_chapter: int):
        return []

    def active_entities(
        self,
        _project_id: str,
        *,
        as_of_chapter: int,
    ) -> list[object]:
        return []


class _AnchoredContinuityChecker(ContinuityChecker):
    """ContinuityChecker with explicit canon name anchors, as BookState will supply them."""

    def __init__(self, anchors: list[CanonNameAnchor]) -> None:
        super().__init__(SimpleNamespace())
        self._anchors = anchors
        self.anchor_reads: list[tuple[str, int]] = []

    def _canon_name_anchors(
        self,
        project_id: str,
        *,
        as_of_chapter: int = 10**9,
    ) -> list[CanonNameAnchor]:
        self.anchor_reads.append((project_id, as_of_chapter))
        return list(self._anchors)


class SubWorldControlTests(unittest.TestCase):

    def test_same_logical_subworld_id_is_isolated_across_projects(self) -> None:
        engine = get_engine(postgres_test_url("subworld-project-isolation"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project_a = updater.create_project(
                title="甲书",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            project_b = updater.create_project(
                title="乙书",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            arc_a = updater.create_arc_plan(project_a.id, "甲弧线")
            arc_b = updater.create_arc_plan(project_b.id, "乙弧线")
            manager = SubWorldManager()

            def delta(role_hint: str) -> SubWorldPlanDelta:
                return SubWorldPlanDelta.model_validate(
                    {
                        "new_subworlds": [
                            {
                                "subworld_id": "arc_local_1",
                                "name": "同名局部世界",
                                "scope": "arc_local",
                                "chapter_window_hint": "1-3",
                                "planned_slots": [
                                    {
                                        "slot_key": "local-contact",
                                        "role_hint": role_hint,
                                        "description": f"{role_hint}的角色槽位",
                                    }
                                ],
                            }
                        ],
                        "initial_active_subworld_ids": ["arc_local_1"],
                    }
                )

            result_a = manager.apply_arc_delta(
                session=session,
                project_id=project_a.id,
                arc_id=arc_a.id,
                delta=delta("甲方联络员"),
                chapter_number=0,
            )
            result_b = manager.apply_arc_delta(
                session=session,
                project_id=project_b.id,
                arc_id=arc_b.id,
                delta=delta("乙方联络员"),
                chapter_number=0,
            )
            session.flush()

            subworld_a = result_a.new_subworlds[0].subworld_id
            subworld_b = result_b.new_subworlds[0].subworld_id
            # Planned slots stay unnamed until the plan supplies a name; only
            # named slots can be activated into entry targets.
            for owner_id, slot_name in ((project_a.id, "甲联络员名"), (project_b.id, "乙联络员名")):
                for slot in session.execute(
                    select(SubWorldRosterItem).where(SubWorldRosterItem.project_id == owner_id)
                ).scalars():
                    slot.display_name = slot_name
            session.flush()
            activation_a = manager.plan_band_activation(
                session=session,
                project_id=project_a.id,
                chapter_start=1,
                chapter_end=3,
                active_band=[],
            )
            activation_b = manager.plan_band_activation(
                session=session,
                project_id=project_b.id,
                chapter_start=1,
                chapter_end=3,
                active_band=[],
            )
            rosters = session.execute(
                select(SubWorldRosterItem).where(
                    SubWorldRosterItem.project_id.in_([project_a.id, project_b.id])
                )
            ).scalars().all()
        finally:
            session.close()
            engine.dispose()

        self.assertNotEqual(subworld_a, subworld_b)
        self.assertNotEqual(subworld_a, "arc_local_1")
        self.assertNotEqual(subworld_b, "arc_local_1")
        self.assertEqual(
            {item.subworld_id for item in rosters if item.project_id == project_a.id},
            {subworld_a},
        )
        self.assertEqual(
            {item.subworld_id for item in rosters if item.project_id == project_b.id},
            {subworld_b},
        )
        self.assertEqual(
            [item.role_hint for item in activation_a.chapter_entry_targets],
            ["甲方联络员"],
        )
        self.assertEqual(
            [item.role_hint for item in activation_b.chapter_entry_targets],
            ["乙方联络员"],
        )

    def test_ensure_registry_rosters_book_state_characters(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("subworld-book-state"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                BookStateRepository(session).create_world_node(
                    WorldNode(
                        id="char_book",
                        project_id=project.id,
                        node_type="character",
                        name="陆沉",
                        description="BookState canon 角色",
                    ),
                    version_chapter=0,
                )
                manager = SubWorldManager()

                global_core_id = manager.ensure_registry(session, project.id)
                roster = session.execute(
                    select(SubWorldRosterItem)
                    .where(SubWorldRosterItem.project_id == project.id)
                ).scalar_one()
                metadata = json.loads(roster.metadata_json)
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(roster.subworld_id, global_core_id)
        self.assertIsNone(roster.entity_id)
        self.assertEqual(roster.display_name, "陆沉")
        self.assertEqual(metadata["character_id"], "char_book")
        self.assertEqual(metadata["canon_source"], "book_state")

    def test_project_detail_reads_book_state_characters(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("project-detail-book-state-characters"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                BookStateRepository(session).create_world_node(
                    WorldNode(
                        id="char_book",
                        project_id=project.id,
                        node_type="character",
                        name="陆沉",
                        description="BookState canon 角色",
                    ),
                    version_chapter=0,
                )

                detail = build_project_detail(
                    session=session,
                    project=project,
                    display_datetime=lambda _value: "",
                )
            finally:
                session.close()
                engine.dispose()

        self.assertEqual([item.id for item in detail.characters], ["char_book"])
        self.assertEqual(detail.characters[0].name, "陆沉")

    def test_arc_director_plan_arc_includes_subworld_delta_and_seed_characters(self) -> None:
        class FakeLLMClient:
            def chat(self, messages, temperature: float, max_tokens: int, response_format=None) -> str:
                return (
                    '{"arc_synopsis":"弧线","setting_summary":"设定","chapters":[{"chapter_number":1,'
                    '"title":"第一章","one_line":"开场","goals":["目标1","目标2"]}],"characters":[],'
                    '"locations":[],"factions":[],"relations":[],"plot_threads":[],"initial_time":'
                    '{"label":"开始","description":"开场"}}'
                )

        director = ArcDirector(FakeLLMClient(), max_tokens=2048)

        plan = director.plan_arc("故事前提", "玄幻", 1)

        self.assertIn("subworld_delta", plan)
        self.assertTrue(plan["subworld_delta"]["new_subworlds"])
        self.assertTrue(any(item["region_seeds"] for item in plan["subworld_delta"]["new_subworlds"] if item.get("scope") == "arc_local"))
        self.assertTrue(plan["characters"])

    def test_arc_director_normalize_chapters_rejects_single_character_goals(self) -> None:
        director = ArcDirector(SimpleNamespace(chat=lambda *args, **kwargs: "{}"), max_tokens=2048)

        chapters = director._normalize_chapters(
            [
                {
                    "chapter_number": 1,
                    "title": "第一章",
                    "one_line": "周岚找到旧港火灾记录。",
                    "goals": ["揭", "示", "周"],
                }
            ],
            1,
            "周岚调查旧港火灾。",
        )

        self.assertEqual(
            chapters[0]["goals"],
            ["推进本章主线冲突", "提供新的线索、代价或反转"],
        )

    def test_arc_director_tolerates_non_numeric_subworld_seed_fields(self) -> None:
        director = ArcDirector(SimpleNamespace(chat=lambda *args, **kwargs: "{}"), max_tokens=2048)

        normalized = director._normalize_subworld_delta(
            {
                "reuse_subworld_ids": [],
                "retire_subworld_ids": [],
                "initial_active_subworld_ids": [],
                "new_subworlds": [
                    {
                        "name": "雾港",
                        "scope": "arc_local",
                        "core_named_characters": [
                            {
                                "name": "林夜",
                                "description": "记录员",
                                "role_hint": "主角",
                                "importance": "protagonist",
                            }
                        ],
                        "planned_slots": [],
                        "region_seeds": [
                            {
                                "name": "旧航线档案馆",
                                "level": "primary",
                            }
                        ],
                    }
                ],
            },
            fallback={"new_subworlds": []},
        )
        merged = director._merge_seed_characters([], normalized["new_subworlds"])

        self.assertEqual(normalized["new_subworlds"][0]["core_named_characters"][0]["importance"], 5)
        self.assertEqual(normalized["new_subworlds"][0]["region_seeds"][0]["level"], 1)
        self.assertEqual(merged[0]["importance"], 5)

    def test_assemble_context_uses_book_state_entities_and_active_subworlds(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("context"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                arc = updater.create_arc_plan(project.id, "弧线")
                updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    one_line="开场",
                    goals=["推进"],
                )
                book_state = BookStateRepository(session)
                book_state.create_world_node(
                    WorldNode(
                        id="char_allowed",
                        project_id=project.id,
                        node_type="character",
                        name="阿青",
                        description="允许角色",
                        profile={
                            "personality_loadout": {
                                "dominant": {
                                    "skill": "trait-loyal-protector",
                                    "weight": 0.72,
                                },
                                "secondary": [],
                                "social_mask": [],
                                "stress_modes": [],
                                "relationship_patterns": [],
                                "overrides": {},
                            }
                        },
                    ),
                    version_chapter=0,
                )
                book_state.create_world_node(
                    WorldNode(
                        id="char_other_subworld",
                        project_id=project.id,
                        node_type="character",
                        name="小明",
                        description="其他子世界角色",
                        profile={
                            "personality_loadout": {
                                "dominant": {
                                    "skill": "trait-loyal-protector",
                                    "weight": 0.72,
                                },
                                "secondary": [],
                                "social_mask": [],
                                "stress_modes": [],
                                "relationship_patterns": [],
                                "overrides": {},
                            }
                        },
                    ),
                    version_chapter=0,
                )
                book_state.create_world_node(
                    WorldNode(
                        id="location_old_house",
                        project_id=project.id,
                        node_type="location",
                        name="旧宅",
                        description="地点",
                    ),
                    version_chapter=0,
                )
                global_core = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=arc.id,
                    parent_subworld_id=None,
                    name="global_core",
                    purpose="核心角色",
                    scope="global_core",
                    metadata={},
                )
                arc_local = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=arc.id,
                    parent_subworld_id=None,
                    name="江城线",
                    purpose="局部舞台",
                    scope="arc_local",
                    metadata={"chapter_window_hint": "1-3"},
                )
                updater.create_roster_item(
                    project_id=project.id,
                    subworld_id=global_core.id,
                    entity_id=None,
                    display_name="阿青",
                    description="允许角色",
                    is_core=True,
                    status="seeded_named",
                    metadata={
                        "character_id": "char_allowed",
                        "book_state_node_id": "char_allowed",
                        "canon_source": "book_state",
                    },
                )
                updater.create_roster_item(
                    project_id=project.id,
                    subworld_id=arc_local.id,
                    entity_id=None,
                    display_name="小明",
                    description="其他子世界角色",
                    is_core=True,
                    status="seeded_named",
                    metadata={
                        "character_id": "char_other_subworld",
                        "book_state_node_id": "char_other_subworld",
                        "canon_source": "book_state",
                    },
                )
                updater.update_chapter_experience_plan(
                    project.id,
                    1,
                    ChapterExperiencePlan(
                        active_subworld_ids=[global_core.id],
                        entity_admission_rule="strict_named_character",
                    ),
                )
                revision = BookGenesisRevision(
                    project_id=project.id,
                    revision=1,
                    status="active",
                    pack_json=json.dumps(
                        {
                            "map_atlas": {
                                "overview": "主世界地图总览",
                                "submaps": [{"name": "主舞台总图"}],
                                "regions": [
                                    {
                                        "id": "region-main",
                                        "name": "主城核心区",
                                        "subworld_name": "主舞台总图",
                                        "parent_region_id": "",
                                        "level": 1,
                                    }
                                ],
                                "nodes": [],
                            }
                        },
                        ensure_ascii=False,
                    ),
                )
                session.add(revision)
                session.flush()
                project.active_genesis_revision_id = revision.id
                global_core.metadata_json = json.dumps(
                    {
                        "region_drafts": [
                            {
                                "name": "江城前哨区",
                                "level": 1,
                                "kind": "frontier_domain",
                            }
                        ],
                        "region_source": "runtime_generated",
                        "region_promotion_state": "draft",
                    },
                    ensure_ascii=False,
                )
                session.add(global_core)
                session.flush()
                repo = StateRepository(session)
                plan = repo.get_chapter_plan(project.id, 1)
                context = assemble_context(repo, project.id, plan)
            finally:
                session.close()
                engine.dispose()

        active_names = [item.name for item in context.active_entities]
        self.assertIn("阿青", active_names)
        self.assertIn("小明", active_names)
        self.assertIn("旧宅", active_names)
        self.assertEqual(set(context.allowed_entities), {"阿青", "小明"})
        self.assertEqual(context.entity_admission_rule, "strict_named_character")
        self.assertIn("Genesis 地区：主城核心区@主舞台总图·L1", context.genesis_map_overview)
        self.assertIn("运行时地区草案：江城前哨区@global_core·L1", context.genesis_map_overview)

    def test_active_subworld_region_drafts_read_current_region_source(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("region-source"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                global_core = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=None,
                    parent_subworld_id=None,
                    name="global_core",
                    purpose="核心舞台",
                    scope="global_core",
                    metadata={},
                )
                session.add(
                    MapRegionRow(
                        id="region-current-source",
                        project_id=project.id,
                        subworld_id=global_core.id,
                        region_type="local_region",
                        name="新舞台核心区",
                        description="地区",
                        metadata_json=json.dumps(
                            {"level": 1, "region_source": "runtime_generated"},
                            ensure_ascii=False,
                        ),
                    )
                )
                session.commit()

                drafts = StateRepository(session).get_active_subworld_region_drafts(project.id, 1)
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(drafts[0]["name"], "新舞台核心区")
        self.assertEqual(drafts[0]["region_source"], "runtime_generated")

    def test_writer_prompt_includes_subworld_rules_and_entry_targets(self) -> None:
        context = SimpleNamespace(
            project_title="书",
            genre="玄幻",
            premise="前提",
            setting_summary="设定",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="开场",
            chapter_goals=["推进主线"],
            previous_chapter_summaries=[],
            active_entities=[SimpleNamespace(name="阿青", description="允许角色")],
            active_threads=[],
            active_relations=[],
            canon_name_anchors=[],
            timeline=None,
            audience_hints=None,
            reader_promise=None,
            arc_payoff_map=None,
            band_delight_schedule=None,
            chapter_experience_plan=None,
            active_subworlds=[SubWorldSummary(id="sw1", name="global_core", scope="global_core")],
            allowed_entities=["阿青"],
            chapter_entry_targets=[ChapterEntryTarget(chapter_hint=1, entity_name="沈知遥", subworld_id="sw2", role_hint="新盟友")],
            entity_admission_rule="strict_named_character",
            chapter_task_contract=[],
            band_task_contract=[],
            active_future_constraints=[],
            next_band_summary=None,
            retrieved_memories=[],
        )

        prompt = build_single_chapter_draft_prompt(context)
        content = prompt[1]["content"]

        self.assertIn("当前允许直接使用的命名人物：阿青", content)
        self.assertIn("沈知遥", content)
        self.assertIn("命名人物只能使用允许名单里的名字", content)

    def test_writer_prompt_includes_recent_thread_beats_without_deriving_name_anchors(self) -> None:
        context = SimpleNamespace(
            project_title="测试书",
            genre="都市悬疑",
            premise="陆明追查母亲留下的空白遗书。",
            setting_summary="临潮有回声账本。",
            chapter_number=3,
            chapter_plan_title="第二日",
            chapter_plan_one_line="提取回声残片",
            chapter_goals=["揭示母亲与回声账本有关"],
            previous_chapter_summaries=["陆明发现母亲留下的空白遗书。"],
            active_entities=[SimpleNamespace(name="陆明", description="主角")],
            active_threads=[
                SimpleNamespace(
                    name="空白遗书",
                    description="母亲线索",
                    recent_beats=["终端显示条目标题为“原型设计者：林若”，即母亲的名字。"],
                )
            ],
            active_relations=[],
            canon_name_anchors=[],
            timeline=None,
            audience_hints=None,
            reader_promise=None,
            arc_payoff_map=None,
            band_delight_schedule=None,
            chapter_experience_plan=None,
            active_subworlds=[],
            allowed_entities=["陆明"],
            chapter_entry_targets=[],
            entity_admission_rule="strict_named_character",
            chapter_task_contract=[],
            band_task_contract=[],
            active_future_constraints=[],
            next_band_summary=None,
            retrieved_memories=[],
        )

        prompt = build_single_chapter_draft_prompt(context)
        content = prompt[1]["content"]

        self.assertIn("原型设计者：林若", content)
        self.assertNotIn("Canon 命名锚点", content)
        self.assertNotIn("母亲姓名", content)
        self.assertIn("不得把前情中已经出现的姓名扩写、替换或另造别名", content)

    def test_canon_name_anchor_section_renders_explicit_anchors_for_any_role(self) -> None:
        section = _canon_name_anchor_section(
            SimpleNamespace(),
            anchors=[
                CanonNameAnchor(role_label="师父", canonical_name="甲乙"),
                CanonNameAnchor(role_label="母亲", canonical_name="丙丁"),
                CanonNameAnchor(role_label="", canonical_name="戊己"),
            ],
        )

        assert section is not None
        self.assertIn("【Canon 命名锚点】", section)
        self.assertIn("师父姓名：甲乙；必须逐字沿用，不得扩写、替换或另造别名。", section)
        self.assertIn("母亲姓名：丙丁", section)
        self.assertNotIn("戊己", section)
        self.assertIsNone(_canon_name_anchor_section(SimpleNamespace()))

    def test_writer_prompt_does_not_drop_active_thread_after_three_threads(self) -> None:
        context = SimpleNamespace(
            project_title="测试书",
            genre="都市悬疑",
            premise="陆明追查母亲留下的空白遗书。",
            setting_summary="临潮有回声账本。",
            chapter_number=3,
            chapter_plan_title="第二日",
            chapter_plan_one_line="提取回声残片",
            chapter_goals=["揭示母亲与回声账本有关"],
            previous_chapter_summaries=["陆明发现母亲留下的空白遗书。"],
            active_entities=[SimpleNamespace(name="陆明", description="主角")],
            active_threads=[
                SimpleNamespace(name="合作与危机", description="", status="resolved", priority=2, recent_beats=["合作线已暂时收束。"]),
                SimpleNamespace(name="母亲线索", description="", status="active", priority=2, recent_beats=["火灾录音确认是母亲的声音。"]),
                SimpleNamespace(name="神秘短信", description="", status="active", priority=2, recent_beats=["短信要求下午三点到旧港。"]),
                SimpleNamespace(name="空白遗书", description="", status="active", priority=2, recent_beats=["终端显示条目标题为“原型设计者：林若”，即母亲的名字。"]),
                SimpleNamespace(name="记忆删除", description="", status="active", priority=2, recent_beats=["公共记忆正在被系统性抹除。"]),
                SimpleNamespace(name="许安出现", description="", status="active", priority=2, recent_beats=["许安提供旧港火灾档案。"]),
                SimpleNamespace(name="陈伯伦消失", description="", status="active", priority=2, recent_beats=["陆明检索陈伯伦，发现记录消失。"]),
            ],
            active_relations=[],
            canon_name_anchors=[],
            timeline=None,
            audience_hints=None,
            reader_promise=None,
            arc_payoff_map=None,
            band_delight_schedule=None,
            chapter_experience_plan=None,
            active_subworlds=[],
            allowed_entities=["陆明"],
            chapter_entry_targets=[],
            entity_admission_rule="strict_named_character",
            chapter_task_contract=[],
            band_task_contract=[],
            active_future_constraints=[],
            next_band_summary=None,
            retrieved_memories=[],
        )

        prompt = build_single_chapter_draft_prompt(context)
        content = prompt[1]["content"]

        self.assertIn("原型设计者：林若", content)
        self.assertNotIn("母亲姓名", content)
        self.assertIn("陈伯伦", content)
        self.assertLess(content.index("母亲线索"), content.index("合作与危机"))

    def test_continuity_checker_warns_on_canon_name_drift_for_explicit_anchors(self) -> None:
        checker = _AnchoredContinuityChecker(
            [CanonNameAnchor(role_label="师父", canonical_name="甲乙")]
        )
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=3,
                title="第二日",
                body="山门前站着师父甲乙丙。他回头看了一眼，甲乙说：“走吧。”" * 30,
                end_of_chapter_summary="弟子认出师父甲乙丁。",
            ),
        )

        issues = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
        observed = {issue.entity_names[0] for issue in issues}
        self.assertEqual(verdict.verdict, "warn")
        self.assertEqual(verdict.recommended_action, "continue")
        self.assertTrue(all(issue.severity == "warning" for issue in issues))
        self.assertEqual(observed, {"甲乙丙", "甲乙丁"})
        self.assertTrue(all(issue.entity_names[1] == "甲乙" for issue in issues))
        self.assertTrue(all("师父" in issue.description for issue in issues))
        self.assertTrue(all("甲乙" in issue.suggested_fix for issue in issues))
        self.assertEqual(checker.anchor_reads, [("p1", 2)])

    def test_continuity_checker_warns_on_canon_name_drift_in_state_metadata(self) -> None:
        checker = _AnchoredContinuityChecker(
            [CanonNameAnchor(role_label="师父", canonical_name="甲乙")]
        )
        body = "师父甲乙留下了线索。" * 80
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=7,
                title="第六日",
                body=body,
                end_of_chapter_summary="弟子确认师父甲乙与旧事有关。",
                chapter_delta=chapter_delta(
                    body,
                    entity_changes=[
                        {
                            "ref": "char-disciple",
                            "field": "knowledge",
                            "new_value": "得知师父甲乙丁。",
                            "evidence": "师父甲乙留下了线索",
                        }
                    ],
                ),
            ),
        )

        issues = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
        self.assertEqual(verdict.verdict, "warn")
        self.assertEqual([(issue.entity_names[0], issue.severity) for issue in issues], [("甲乙丁", "warning")])
        self.assertEqual(checker.anchor_reads, [("p1", 6)])

    def test_continuity_checker_has_no_canon_name_anchors_without_kinship_edges(self) -> None:
        book_state = _BookStateQueryStub(
            ["终端显示条目标题为“原型设计者：林若”，即母亲的名字。"]
        )
        checker = ContinuityChecker(
            SimpleNamespace(),
            book_state_query=book_state,
        )
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=3,
                title="第二日",
                body="周岚说：“你母亲叫林婉清。”残片里又有人称她为林若水。" * 30,
                end_of_chapter_summary="陆明发现母亲林婉清是设计者。",
            ),
        )

        self.assertEqual(
            [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"],
            [],
        )
        self.assertEqual(book_state.thread_reads, [])

    def test_canon_name_violations_are_role_agnostic(self) -> None:
        for role in ("师父", "母亲", "盟友"):
            with self.subTest(role=role):
                violations = find_canon_name_violations(
                    [CanonNameAnchor(role_label=role, canonical_name="甲乙")],
                    "山门前站着甲乙丙。",
                )

                self.assertEqual(len(violations), 1)
                violation = violations[0]
                self.assertEqual(violation.role_label, role)
                self.assertEqual(violation.canonical_name, "甲乙")
                self.assertEqual(violation.observed_name, "甲乙丙")
                self.assertEqual(violation.reason, "expanded_canonical_name")
                self.assertIn("甲乙丙", violation.evidence)

    def test_canon_name_violations_cover_every_anchor_once_per_observed_name(self) -> None:
        violations = find_canon_name_violations(
            [
                CanonNameAnchor(role_label="师父", canonical_name="甲乙"),
                CanonNameAnchor(role_label="盟友", canonical_name="丙丁"),
            ],
            "师父甲乙丙。盟友丙丁戊。后来又提到师父甲乙丙。",
        )

        self.assertEqual(
            [(v.role_label, v.observed_name) for v in violations],
            [("师父", "甲乙丙"), ("盟友", "丙丁戊")],
        )

    def test_canon_name_violations_allow_the_canonical_name_in_ordinary_prose(self) -> None:
        violations = find_canon_name_violations(
            [CanonNameAnchor(role_label="师父", canonical_name="甲乙")],
            "弟子拜见师父甲乙，也找到了甲乙的剑。甲乙说：“进来。”这把剑是甲乙的。",
        )

        self.assertEqual(violations, [])

    def test_canon_name_violations_ignore_function_word_continuations(self) -> None:
        for text in (
            "说的就是甲乙了。",
            "他们只提到甲乙和。",
            "没人提起甲乙他。",
            "不会是甲乙不是。",
        ):
            with self.subTest(text=text):
                violations = find_canon_name_violations(
                    [CanonNameAnchor(role_label="师父", canonical_name="甲乙")],
                    text,
                )

                self.assertEqual(violations, [])

    def test_canon_name_violations_need_usable_anchors_and_text(self) -> None:
        anchor = CanonNameAnchor(role_label="师父", canonical_name="甲乙")

        self.assertEqual(find_canon_name_violations([], "师父甲乙丙。"), [])
        self.assertEqual(find_canon_name_violations([anchor], ""), [])
        self.assertEqual(
            find_canon_name_violations(
                [
                    CanonNameAnchor(role_label="", canonical_name="甲乙"),
                    CanonNameAnchor(role_label="师父", canonical_name=""),
                ],
                "师父甲乙丙。",
            ),
            [],
        )

    def test_canon_name_drift_autofix_replaces_observed_name_across_writer_output(self) -> None:
        body = "旧港是母亲最后出现的地方。陆明发现母亲林婉是原型设计者，报告撰写人是林婉。"
        output = WriterOutput(
            chapter_number=3,
            title="第二日",
            body=body,
            end_of_chapter_summary="陆明在记忆馆发现母亲林婉是回声账本原型设计者之一。",
            chapter_delta=chapter_delta(
                body,
                events=[
                    {
                        "summary": "陆明发现林婉留下技术报告",
                        "participant_refs": ["char-luming", "new:林婉"],
                        "significance": "major",
                        "visibility": "public",
                        "evidence": "陆明发现母亲林婉是原型设计者",
                    }
                ],
            ),
        )
        review = ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="母亲姓名漂移。",
                    entity_names=["林婉", "林若"],
                )
            ],
        )

        fixed = ChapterPipeline._apply_canon_name_drift_autofix(output, review)

        self.assertIsNotNone(fixed)
        assert fixed is not None
        serialized_content = fixed.model_dump_json(exclude={"generation_meta"})
        self.assertNotIn("林婉", serialized_content)
        self.assertIn("林若", fixed.body)
        self.assertIn("母亲最后出现的地方", fixed.body)
        self.assertEqual(fixed.char_count, len(fixed.body))
        self.assertEqual(fixed.generation_meta["canon_name_autofix"]["林婉"], "林若")

    def test_canon_name_drift_autofix_ignores_non_name_observed_terms(self) -> None:
        output = WriterOutput(
            chapter_number=13,
            title="旧港的余烬",
            body="许安说：“因为是我帮你母亲申请的工位。”这是他和母亲之间的暗号。录音结束。",
            end_of_chapter_summary="陆明和许安发现旧港线索。",
        )
        review = ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="误报的母亲姓名漂移。",
                    entity_names=["本人", "林若"],
                ),
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="误报的母亲姓名漂移。",
                    entity_names=["之间", "林若"],
                ),
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="误报的母亲姓名漂移。",
                    entity_names=["录音", "林若"],
                ),
            ],
        )

        fixed = ChapterPipeline._apply_canon_name_drift_autofix(output, review)

        self.assertIsNone(fixed)

    def test_canon_name_drift_autofix_ignores_expanded_canonical_phrase(self) -> None:
        output = WriterOutput(
            chapter_number=13,
            title="旧港的余烬",
            body="母亲林若警告他不要相信回声账本。",
            end_of_chapter_summary="陆明发现母亲留下警告。",
        )
        review = ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="误报的母亲姓名扩展。",
                    entity_names=["林若警告", "林若"],
                )
            ],
        )

        fixed = ChapterPipeline._apply_canon_name_drift_autofix(output, review)

        self.assertIsNone(fixed)

    def test_rearc_creates_new_subworlds_via_director_delta(self) -> None:
        class FakeDirector:
            def plan_subworld_delta(self, **kwargs):
                return {
                    "reuse_subworld_ids": [kwargs["existing_subworlds"][0].id],
                    "retire_subworld_ids": [],
                    "new_subworlds": [
                        {
                            "name": "新舞台",
                            "purpose": "rearc 后的新局部舞台",
                            "scope": "arc_local",
                            "chapter_window_hint": "2-4",
                            "region_seeds": [
                                {
                                    "name": "新舞台核心区",
                                    "level": 1,
                                    "kind": "frontier_domain",
                                    "parent_region_name": "",
                                    "summary": "新舞台的核心地区",
                                    "culture_traits": ["高压边境"],
                                    "climate": "寒冷干燥",
                                    "terrain": ["山地"],
                                    "controller_factions": ["新舞台势力"],
                                }
                            ],
                            "core_named_characters": [
                                {
                                    "name": "沈知遥",
                                    "description": "新的核心人物",
                                    "role_hint": "新盟友",
                                    "importance": 8,
                                }
                            ],
                            "planned_slots": [],
                        }
                    ],
                    "initial_active_subworld_ids": [kwargs["existing_subworlds"][0].id],
                }

        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("rearc"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                old_arc = updater.create_arc_plan(project.id, "旧弧线", version=1)
                for number in (1, 2, 3):
                    updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=old_arc.id,
                        chapter_number=number,
                        title=f"第{number}章",
                        one_line="推进",
                        goals=["推进"],
                    )
                manager = SubWorldManager()
                manager.ensure_registry(session, project.id)
                governor = ReplanGovernor(
                    cooldown_chapters=3,
                    director=FakeDirector(),
                    subworld_manager=manager,
                )

                governor._apply_rearc(
                    session=session,
                    project_id=project.id,
                    chapter_number=1,
                    stage=StageAssessment("midpoint", 0.5, "", 0),
                    focus_threads=["旧线"],
                )

                active_arcs = session.execute(
                    select(ChapterPlan.arc_plan_id).where(ChapterPlan.project_id == project.id, ChapterPlan.chapter_number > 1)
                ).all()
                subworld_names = [
                    row.name
                    for row in session.execute(
                        select(SubWorld).where(SubWorld.project_id == project.id)
                    ).scalars().all()
                ]
                active_arc_rows = session.execute(
                    select(SubWorld.origin_arc_id, SubWorld.name)
                    .where(SubWorld.project_id == project.id, SubWorld.name == "新舞台")
                ).all()
                new_subworld = session.execute(
                    select(SubWorld)
                    .where(SubWorld.project_id == project.id, SubWorld.name == "新舞台")
                ).scalar_one()
                new_region = session.execute(
                    select(MapRegionRow)
                    .where(MapRegionRow.project_id == project.id, MapRegionRow.subworld_id == new_subworld.id)
                ).scalar_one()
            finally:
                session.close()
                engine.dispose()

        self.assertIn("新舞台", subworld_names)
        self.assertEqual(len({arc_id for arc_id, in active_arcs}), 1)
        self.assertTrue(active_arc_rows)
        metadata = json.loads(new_subworld.metadata_json or "{}")
        self.assertEqual(metadata.get("region_source"), "runtime_generated")
        self.assertEqual(metadata.get("region_promotion_state"), "draft")
        self.assertEqual(metadata.get("region_drafts", [])[0]["name"], "新舞台核心区")
        region_metadata = json.loads(new_region.metadata_json or "{}")
        self.assertEqual(region_metadata.get("region_source"), "runtime_generated")
        self.assertNotIn("legacy_source", region_metadata)

    def test_phase24_persists_subworld_activation_into_band_and_chapter_plan(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("phase24"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                arc = updater.create_arc_plan(project.id, "弧线")
                plans = [
                    updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=number,
                        title=f"第{number}章",
                        one_line="推进江城线",
                        goals=["推进江城线"],
                    )
                    for number in (1, 2, 3)
                ]
                manager = SubWorldManager()
                BookStateRepository(session).create_world_node(
                    WorldNode(
                        id="char_core_qing",
                        project_id=project.id,
                        node_type="character",
                        name="阿青",
                        description="主角团成员",
                    ),
                    version_chapter=0,
                )
                manager.ensure_registry(session, project.id)
                arc_local = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=arc.id,
                    parent_subworld_id=None,
                    name="江城线",
                    purpose="推进江城线",
                    scope="arc_local",
                    metadata={"chapter_window_hint": "1-3"},
                )
                updater.create_roster_item(
                    project_id=project.id,
                    subworld_id=arc_local.id,
                    entity_id=None,
                    display_name="林舟",
                    slot_key="jiangcheng-contact",
                    role_hint="江城关键联系人",
                    description="本段首次进入视野的联系人",
                    is_core=False,
                    status="planned_slot",
                )
                envelope_manager = ArcEnvelopeManager(subworld_manager=manager)

                envelope_manager._persist_experience_overlay(
                    session=session,
                    project_id=project.id,
                    arc_id=arc.id,
                    chapter_plans=plans,
                    activation_chapter=1,
                    detailed_band_size=3,
                    structure=ArcStructureDraftData(
                        phase_layout=["setup", "pressure", "turn"],
                        key_beats=["江城异动", "调查推进", "真相前夜"],
                        thread_priorities=[],
                        hotspot_candidates=[],
                        compression_candidates=[],
                        reader_promise=ReaderPromise(genre_promise="玄幻"),
                        arc_payoff_map=ArcPayoffMap(),
                    ),
                )

                band_row = session.execute(select(BandExperiencePlan)).scalar_one()
                band_payload = json.loads(band_row.schedule_json or "{}")
                chapter_plan = session.execute(
                    select(ChapterPlan)
                    .where(ChapterPlan.project_id == project.id, ChapterPlan.chapter_number == 1)
                ).scalar_one()
                chapter_payload = json.loads(chapter_plan.experience_plan_json or "{}")
            finally:
                session.close()
                engine.dispose()

        self.assertTrue(band_payload.get("active_subworld_ids"))
        self.assertTrue(band_payload.get("chapter_entry_targets"))
        self.assertEqual(chapter_payload.get("entity_admission_rule"), "strict_named_character")
        self.assertTrue(chapter_payload.get("active_subworld_ids"))

    def test_project_detail_exposes_subworld_summaries(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("detail"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                global_core = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=None,
                    parent_subworld_id=None,
                    name="global_core",
                    purpose="核心",
                    scope="global_core",
                    metadata={},
                )
                updater.create_roster_item(
                    project_id=project.id,
                    subworld_id=global_core.id,
                    entity_id=None,
                    display_name="阿青",
                    description="核心角色",
                    is_core=True,
                    status="planned_slot",
                )
                detail = build_project_detail(
                    session=session,
                    project=project,
                    display_datetime=lambda dt: "",
                )
            finally:
                session.close()
                engine.dispose()

        self.assertTrue(detail.subworlds)
        self.assertEqual(detail.subworlds[0].name, "global_core")

    def test_band_activation_reserves_entry_name_without_writing_book_state(self) -> None:
        engine = get_engine(postgres_test_url("planned-slot-canon-boundary"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="书",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            subworld = updater.create_subworld(
                project_id=project.id,
                origin_arc_id=None,
                parent_subworld_id=None,
                name="江城",
                purpose="本地线",
                scope="arc_local",
                metadata={"chapter_window_hint": "3-5"},
            )
            roster = updater.create_roster_item(
                project_id=project.id,
                subworld_id=subworld.id,
                entity_id=None,
                display_name="阿青",
                slot_key="guard",
                role_hint="护卫",
                description="冷静护卫，负责保护主角。",
                status="planned_slot",
            )

            activation = SubWorldManager().plan_band_activation(
                session=session,
                project_id=project.id,
                chapter_start=3,
                chapter_end=5,
                active_band=[
                    SimpleNamespace(
                        title="江城",
                        one_line="护卫进入本地线",
                        goals_json='["引入护卫"]',
                    )
                ],
            )
            session.flush()
            session.refresh(roster)
            metadata = json.loads(roster.metadata_json or "{}")
            nodes = BookStateRepository(session).list_world_nodes(project.id)
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(len(activation.chapter_entry_targets), 1)
        self.assertEqual(activation.chapter_entry_targets[0].entity_name, "阿青")
        self.assertEqual(roster.display_name, "阿青")
        self.assertEqual(nodes, [])
        self.assertEqual(roster.status, "activated_named")
        self.assertEqual(metadata.get("entry_target_chapter"), 3)
        self.assertTrue(metadata.get("pending_entity_admission"))
        self.assertNotIn("character_id", metadata)
        self.assertNotIn("book_state_node_id", metadata)

    def test_band_activation_skips_unnamed_planned_slots(self) -> None:
        engine = get_engine(postgres_test_url("planned-slot-unnamed-skip"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="书",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            subworld = updater.create_subworld(
                project_id=project.id,
                origin_arc_id=None,
                parent_subworld_id=None,
                name="江城",
                purpose="本地线",
                scope="arc_local",
                metadata={"chapter_window_hint": "3-5"},
            )
            named = updater.create_roster_item(
                project_id=project.id,
                subworld_id=subworld.id,
                entity_id=None,
                display_name="阿青",
                slot_key="named-slot",
                role_hint="护卫",
                description="有名字的槽位。",
                status="planned_slot",
            )
            unnamed = updater.create_roster_item(
                project_id=project.id,
                subworld_id=subworld.id,
                entity_id=None,
                slot_key="unnamed-slot",
                role_hint="联络人",
                description="没有名字的槽位。",
                status="planned_slot",
            )

            activation = SubWorldManager().plan_band_activation(
                session=session,
                project_id=project.id,
                chapter_start=3,
                chapter_end=5,
                active_band=[
                    SimpleNamespace(
                        title="江城",
                        one_line="本地线展开",
                        goals_json='["引入角色"]',
                    )
                ],
            )
            session.flush()
            session.refresh(named)
            session.refresh(unnamed)
            unnamed_metadata = json.loads(unnamed.metadata_json or "{}")
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(
            [target.entity_name for target in activation.chapter_entry_targets],
            ["阿青"],
        )
        self.assertEqual(named.status, "activated_named")
        self.assertEqual(unnamed.status, "planned_slot")
        self.assertEqual(unnamed.display_name, "")
        self.assertFalse(unnamed.activation_chapter)
        self.assertNotIn("pending_entity_admission", unnamed_metadata)
        self.assertNotIn("entry_target_chapter", unnamed_metadata)

    def test_band_activation_does_not_reintroduce_canon_bound_roster_item(self) -> None:
        engine = get_engine(postgres_test_url("planned-slot-canonical-metadata"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="书",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            subworld = updater.create_subworld(
                project_id=project.id,
                origin_arc_id=None,
                parent_subworld_id=None,
                name="江城",
                purpose="本地线",
                scope="arc_local",
                metadata={"chapter_window_hint": "3-5"},
            )
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="char_canonical_zhou",
                    project_id=project.id,
                    node_type="character",
                    name="周怀瑾",
                    aliases=[],
                    description="负责联络内城。",
                    profile={
                        "personality_loadout": {
                            "dominant": {"skill": "trait-loyal-protector", "weight": 0.9},
                            "secondary": [],
                            "social_mask": [],
                            "stress_modes": [],
                            "relationship_patterns": [],
                            "overrides": {},
                        }
                    },
                    metadata={
                        "character_identity": {
                            "canonical_character_id": "char_canonical_zhou",
                            "book_state_node_id": "char_canonical_zhou",
                        }
                    },
                ),
                version_chapter=0,
            )
            roster = updater.create_roster_item(
                project_id=project.id,
                subworld_id=subworld.id,
                entity_id=None,
                display_name="周执事",
                role_hint="联络人",
                description="负责联络内城。",
                is_core=True,
                status="planned_slot",
                metadata={
                    "character_id": "char_canonical_zhou",
                    "book_state_node_id": "char_canonical_zhou",
                    "canon_source": "book_state",
                },
            )

            activation = SubWorldManager().plan_band_activation(
                session=session,
                project_id=project.id,
                chapter_start=3,
                chapter_end=5,
                active_band=[
                    SimpleNamespace(
                        title="江城",
                        one_line="联络人继续本地线",
                        goals_json='["推进联络"]',
                    )
                ],
            )
            session.flush()
            session.refresh(roster)
            nodes = BookStateRepository(session).list_world_nodes(project.id)
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(activation.chapter_entry_targets, [])
        self.assertEqual([node.id for node in nodes if node.node_type == "character"], ["char_canonical_zhou"])
        self.assertIsNone(roster.entity_id)
        self.assertEqual(roster.display_name, "周执事")

    def test_runtime_subworld_core_character_stays_planned_until_canon(self) -> None:
        engine = get_engine(postgres_test_url("subworld-core-canon-boundary"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="书",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            arc = updater.create_arc_plan(project.id, "弧线")
            manager = SubWorldManager()
            manager.apply_arc_delta(
                session=session,
                project_id=project.id,
                arc_id=arc.id,
                chapter_number=1,
                delta=SubWorldPlanDelta.model_validate(
                    {
                        "new_subworlds": [
                            {
                                "name": "江城",
                                "scope": "arc_local",
                                "core_named_characters": [
                                    {
                                        "name": "沈临川",
                                        "description": "冷静护卫，负责保护主角。",
                                        "role_hint": "护卫",
                                        "importance": 7,
                                    }
                                ],
                            }
                        ],
                        "initial_active_subworld_ids": [],
                    }
                ),
            )
            session.flush()
            nodes = BookStateRepository(session).list_world_nodes(project.id)
            roster = session.execute(select(SubWorldRosterItem).where(SubWorldRosterItem.display_name == "沈临川")).scalar_one()
            roster_metadata = json.loads(roster.metadata_json or "{}")
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(nodes, [])
        self.assertEqual(roster.display_name, "沈临川")
        self.assertTrue(roster_metadata.get("pending_entity_admission"))
        self.assertNotIn("character_id", roster_metadata)
        self.assertNotIn("book_state_node_id", roster_metadata)

    def test_chapter_zero_subworld_core_character_waits_for_entity_admission(self) -> None:
        engine = get_engine(postgres_test_url("subworld-core-chapter-zero"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="书",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            arc = updater.create_arc_plan(project.id, "弧线")
            SubWorldManager().apply_arc_delta(
                session=session,
                project_id=project.id,
                arc_id=arc.id,
                chapter_number=0,
                delta=SubWorldPlanDelta.model_validate(
                    {
                        "new_subworlds": [
                            {
                                "name": "江城",
                                "scope": "arc_local",
                                "core_named_characters": [
                                    {
                                        "name": "沈临川",
                                        "description": "冷静护卫，负责保护主角。",
                                        "role_hint": "护卫",
                                        "importance": 7,
                                    }
                                ],
                            }
                        ]
                    }
                ),
            )
            nodes = BookStateRepository(session).list_world_nodes(project.id)
            roster = session.execute(
                select(SubWorldRosterItem).where(
                    SubWorldRosterItem.display_name == "沈临川"
                )
            ).scalar_one()
            roster_metadata = json.loads(roster.metadata_json or "{}")
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(nodes, [])
        self.assertEqual(
            roster_metadata,
            {"pending_entity_admission": True, "planned_at_chapter": 0},
        )

    def test_runtime_subworld_core_character_roster_dedupes_by_planned_name(self) -> None:
        engine = get_engine(postgres_test_url("subworld-core-roster-planned-dedupe"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="书",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            arc = updater.create_arc_plan(project.id, "弧线")
            manager = SubWorldManager()
            delta = SubWorldPlanDelta.model_validate(
                {
                    "new_subworlds": [
                        {
                            "subworld_id": "subworld_dedupe",
                            "name": "江城",
                            "scope": "arc_local",
                            "core_named_characters": [
                                {
                                    "name": "沈临川",
                                    "description": "冷静护卫，负责保护主角。",
                                    "role_hint": "护卫",
                                    "importance": 7,
                                }
                            ],
                        }
                    ],
                    "initial_active_subworld_ids": [],
                }
            )
            manager.apply_arc_delta(
                session=session,
                project_id=project.id,
                arc_id=arc.id,
                chapter_number=1,
                delta=delta,
            )
            manager.apply_arc_delta(
                session=session,
                project_id=project.id,
                arc_id=arc.id,
                chapter_number=1,
                delta=delta,
            )
            session.flush()
            roster_rows = session.execute(
                select(SubWorldRosterItem).where(
                    SubWorldRosterItem.project_id == project.id,
                    SubWorldRosterItem.subworld_id
                    == project_scoped_subworld_id(project.id, "subworld_dedupe"),
                    SubWorldRosterItem.display_name == "沈临川",
                )
            ).scalars().all()
            nodes = BookStateRepository(session).list_world_nodes(project.id)
            roster_metadata = [json.loads(row.metadata_json or "{}") for row in roster_rows]
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(nodes, [])
        self.assertEqual(len(roster_rows), 1)
        self.assertTrue(roster_metadata[0].get("pending_entity_admission"))
        self.assertNotIn("character_id", roster_metadata[0])


if __name__ == "__main__":
    unittest.main()
