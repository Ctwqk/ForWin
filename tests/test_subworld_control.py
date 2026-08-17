from __future__ import annotations

import json
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sqlalchemy import select

from forwin.application.read_models import build_project_detail
from forwin.book_state import BookStateRepository
from forwin.canon_names import CanonNameAnchor, extract_canon_name_anchors, find_canon_name_violations
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
from forwin.protocol.state_change import EventCandidate, StateChangeCandidate
from forwin.protocol.review import ContinuityIssue
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.subworld_manager import SubWorldManager
from forwin.writer.chapter_writer import ChapterWriter
from tests.postgres import postgres_test_url
from forwin.writer.prompt_core import build_single_chapter_draft_prompt


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

    def active_entities(
        self,
        _project_id: str,
        *,
        as_of_chapter: int,
    ) -> list[object]:
        return []


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
                    )
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
                    )
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
                    )
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
                    )
                )
                book_state.create_world_node(
                    WorldNode(
                        id="location_old_house",
                        project_id=project.id,
                        node_type="location",
                        name="旧宅",
                        description="地点",
                    )
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
            timeline=None,
            world_pressure=None,
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

    def test_writer_prompt_includes_recent_thread_beats_for_canon_names(self) -> None:
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
            timeline=None,
            world_pressure=None,
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
        self.assertIn("母亲姓名：林若", content)
        self.assertIn("不得把前情中已经出现的姓名扩写、替换或另造别名", content)

    def test_writer_prompt_does_not_drop_canon_name_thread_after_three_threads(self) -> None:
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
            timeline=None,
            world_pressure=None,
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
        self.assertIn("母亲姓名：林若", content)
        self.assertIn("陈伯伦", content)
        self.assertLess(content.index("母亲线索"), content.index("合作与危机"))

    def test_continuity_checker_rejects_canon_mother_name_drift(self) -> None:
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
                body=(
                    "韩砚说：“你母亲叫林静安，十年前是临潮集团算法部的核心成员。”"
                    "残片里又有人称她为林若水。"
                )
                * 30,
                end_of_chapter_summary="陆明确认母亲林静安是回声账本设计者。",
            ),
        )

        issues = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
        observed = {issue.entity_names[0] for issue in issues}
        self.assertEqual(verdict.verdict, "fail")
        self.assertIn("林静安", observed)
        self.assertIn("林若水", observed)
        self.assertTrue(all("林若" in issue.suggested_fix for issue in issues))
        self.assertEqual(book_state.thread_reads, [("p1", 2)])

    def test_continuity_checker_rejects_canon_mother_name_drift_in_state_metadata(self) -> None:
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
                chapter_number=7,
                title="第六日",
                body=("陆明确认母亲林若留下了协议覆写线索。") * 80,
                end_of_chapter_summary="陆明确认母亲林若与协议真相有关。",
                state_changes=[
                    StateChangeCandidate(
                        entity_name="陆明",
                        entity_kind="character",
                        field="knowledge",
                        old_value="未知母亲参与协议修改",
                        new_value="得知母亲林清和在旧港火灾当晚登录集团系统并启动继承协议覆写",
                        reason="阿棠修复协议数据后发现母亲林清和的授权时间戳",
                    )
                ],
            ),
        )

        issues = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
        self.assertEqual(verdict.verdict, "fail")
        self.assertEqual([issue.entity_names[0] for issue in issues], ["林清和"])
        self.assertEqual(book_state.thread_reads, [("p1", 6)])

    def test_canon_name_anchor_ignores_role_title_as_mother_name(self) -> None:
        anchors = extract_canon_name_anchors(
            [
                "污染过的线程摘要写成：首席架构，即母亲。",
                "终端显示条目标题为“原型设计者：林若”，即母亲的名字。",
            ]
        )

        self.assertEqual([anchor.canonical_name for anchor in anchors], ["林若"])

    def test_canon_name_observation_ignores_mother_possessive_noun(self) -> None:
        violations = find_canon_name_violations(
            "许安说：“你母亲的遗书是空白的。但如果她留下了其他线索——”",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_mother_event_phrase(self) -> None:
        violations = find_canon_name_violations(
            "他不知道自己为什么会有这个能力，只知道十年前母亲失踪后，他就开始能看到残片。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_mother_last_appearance_phrase(self) -> None:
        violations = find_canon_name_violations(
            "旧港是旧港火灾发生的地方，也是母亲最后出现的地方。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_mother_negative_description(self) -> None:
        violations = find_canon_name_violations(
            "韩砚说：“你母亲不是普通的档案管理员。”",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_mother_ordinary_verb_phrases(self) -> None:
        violations = find_canon_name_violations(
            (
                "许安说：“因为是我帮你母亲申请的工位。”"
                "档案显示，你母亲是外包的档案修复师。"
                "这是他和母亲之间的暗号。"
            ),
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_verb_phrase(self) -> None:
        violations = find_canon_name_violations(
            "这块设备的核心数据林若还在，但需要专业设备才能读取。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_disappear_verb(self) -> None:
        violations = find_canon_name_violations(
            "十年前，旧港火灾，母亲失踪，林若消失，所有线索被拧在一起。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_time_preposition(self) -> None:
        violations = find_canon_name_violations(
            "他记得陆明的遗书提到母亲林若在十年前失踪。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_metadata_nouns(self) -> None:
        violations = find_canon_name_violations(
            "实验对象为林若本人。五人决定前往旧港三号码头寻找林若备份。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_match_verb(self) -> None:
        violations = find_canon_name_violations(
            "旧港火灾目击记录显示，一名女性目击者的描述与林若吻合。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_sit_verb(self) -> None:
        violations = find_canon_name_violations(
            "屏幕转为一段视频，母亲林若坐在实验室里，背景是一排服务器机架。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_role_relation(self) -> None:
        violations = find_canon_name_violations(
            "审查元数据写道：灰衣男声称自己是林若下属，提供旧港火灾前监控和录音。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_allows_canonical_name_in_normal_sentence(self) -> None:
        violations = find_canon_name_violations(
            "陆明确认母亲林若是回声账本原型设计者之一，也找到了林若的签名。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_reports_wrong_name_before_possessive_fact(self) -> None:
        violations = find_canon_name_violations(
            "陆明从残片中提取到母亲林清漪的声音残片，确认她是回声账本原型设计者。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual([violation.observed_name for violation in violations], ["林清漪"])

    def test_canon_name_observation_reports_wrong_name_before_dash_clause(self) -> None:
        violations = find_canon_name_violations(
            "韩砚说：“你母亲苏晚晴——不是普通的档案管理员。”",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual([violation.observed_name for violation in violations], ["苏晚晴"])

    def test_canon_name_observation_reports_wrong_name_after_qi_mother_prefix(self) -> None:
        violations = find_canon_name_violations(
            "韩砚告知陆明其母亲苏晚晴是回声系统原型设计者之一。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual([violation.observed_name for violation in violations], ["苏晚晴"])

    def test_canon_name_observation_reports_wrong_name_before_parenthetical_alias(self) -> None:
        violations = find_canon_name_violations(
            "档案残片显示母亲苏敏（林若溪）曾参与原型设计。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(
            [violation.observed_name for violation in violations],
            ["林若溪", "苏敏"],
        )

    def test_canon_name_observation_reports_document_name_in_mother_context(self) -> None:
        violations = find_canon_name_violations(
            (
                "韩砚说：“你母亲留下的不只是遗书，还有一份她亲手签名的协议。”"
                "陆明看到纸上的内容——一个签名栏，签名栏上方是打印体的名字：林清漪。"
            ),
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual([violation.observed_name for violation in violations], ["林清漪"])

    def test_canon_name_observation_reports_name_followed_by_mother_apposition(self) -> None:
        violations = find_canon_name_violations(
            (
                "算法签名。\n\n"
                "一个名字出现在解析结果的末尾：叶知秋。\n\n"
                "陆明的母亲。"
            ),
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual([violation.observed_name for violation in violations], ["叶知秋"])

    def test_canon_name_observation_reports_name_followed_by_mother_signature(self) -> None:
        violations = find_canon_name_violations(
            (
                "陆明看到家属签字栏里那个名字时，胸腔里的空气像是被抽走了。\n\n"
                "林婉清。\n\n"
                "母亲的签名。他认得这笔字。"
            ),
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual([violation.observed_name for violation in violations], ["林婉清"])

    def test_canon_name_observation_reports_dash_appositive_mother_name(self) -> None:
        violations = find_canon_name_violations(
            "档案上印着一个人名：苏瑾——陆明母亲的名字，标注为主架构师。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual([violation.observed_name for violation in violations], ["苏瑾"])

    def test_canon_name_observation_reports_mother_name_after_separator(self) -> None:
        violations = find_canon_name_violations(
            "手术同意书的家属签字栏——林若母亲的名字，林薇。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual([violation.observed_name for violation in violations], ["林薇"])

    def test_canon_name_observation_ignores_breath_before_canonical_name_apposition(self) -> None:
        violations = find_canon_name_violations(
            "陆明的呼吸林若。母亲的名字像一根针，精准地刺进他不敢触碰的区域。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_name_noun(self) -> None:
        violations = find_canon_name_violations(
            "音频残片中断前，母亲叫林若名字卡在噪音里。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_canonical_name_followed_by_speech_noun(self) -> None:
        violations = find_canon_name_violations(
            "他想起母亲教他修复档案时林若话：“每一条痕迹都有代价。”",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_verb_pronoun_before_canonical_name(self) -> None:
        violations = find_canon_name_violations(
            "项目立项书，署林若你母亲的名字——林若。",
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_canon_name_observation_ignores_signature_followed_by_verb_phrase(self) -> None:
        violations = find_canon_name_violations(
            (
                "陆明反复回放着母亲最后的口型。"
                "周岚把医疗记录摊开在台面上，指着手术同意书上的签名：“林若下这份同意书的时间，是旧港火灾发生后的第二天。”"
            ),
            [CanonNameAnchor(role_label="母亲", canonical_name="林若")],
        )

        self.assertEqual(violations, [])

    def test_continuity_checker_ignores_legacy_repo_threads_without_book_state(self) -> None:
        class LegacyRepo:
            def get_active_threads(self, _project_id: str) -> list[object]:
                return [
                    SimpleNamespace(
                        description="",
                        recent_beats=[
                            "污染过的线程摘要写成：首席架构，即母亲。",
                            "终端显示条目标题为“原型设计者：林若”，即母亲的名字。",
                        ],
                    )
                ]

        checker = ContinuityChecker(LegacyRepo())
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=3,
                title="第二日",
                body=("许安说：“你母亲的遗书是空白的。”周岚说：“你母亲叫林婉清。”") * 30,
                end_of_chapter_summary="陆明发现母亲林婉清是回声账本设计者。",
            ),
        )

        issues = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
        self.assertEqual(verdict.verdict, "warn")
        self.assertEqual(issues, [])

    def test_canon_name_drift_autofix_replaces_observed_name_across_writer_output(self) -> None:
        output = WriterOutput(
            chapter_number=3,
            title="第二日",
            body="旧港是母亲最后出现的地方。陆明发现母亲林婉是原型设计者，报告撰写人是林婉。",
            end_of_chapter_summary="陆明在记忆馆发现母亲林婉是回声账本原型设计者之一。",
            new_events=[
                EventCandidate(
                    summary="陆明发现林婉留下技术报告",
                    significance="major",
                    involved_entity_names=["陆明", "林婉"],
                )
            ],
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
                    entity_names=["申请", "林若"],
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
                    )
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
        self.assertEqual(
            activation.chapter_entry_targets[0].entity_name,
            roster.display_name,
        )
        self.assertEqual(nodes, [])
        self.assertEqual(roster.status, "activated_named")
        self.assertEqual(metadata.get("entry_target_chapter"), 3)
        self.assertTrue(metadata.get("pending_entity_admission"))
        self.assertNotIn("character_id", metadata)
        self.assertNotIn("book_state_node_id", metadata)

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
                )
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

    def test_genesis_subworld_core_character_materializes_at_chapter_zero(self) -> None:
        engine = get_engine(postgres_test_url("subworld-core-genesis-seed"))
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
            node = BookStateRepository(session).list_world_nodes(project.id)[0]
            roster = session.execute(
                select(SubWorldRosterItem).where(
                    SubWorldRosterItem.display_name == "沈临川"
                )
            ).scalar_one()
            roster_metadata = json.loads(roster.metadata_json or "{}")
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(node.created_at_chapter, 0)
        self.assertEqual(
            node.profile["personality_loadout"]["dominant"]["skill"],
            "trait-loyal-protector",
        )
        self.assertEqual(roster_metadata["character_id"], node.id)
        self.assertEqual(roster_metadata["canon_source"], "book_state")

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

    def test_writer_output_parses_entity_mentions(self) -> None:
        writer = ChapterWriter(llm_client=SimpleNamespace(chat=lambda *args, **kwargs: "{}"))
        output = writer._writer_output_from_dict(
            context=SimpleNamespace(project_id="p1", chapter_number=1),
            data={
                "title": "第一章",
                "body": "正文",
                "end_of_chapter_summary": "总结",
                "entity_mentions": [
                    {
                        "entity_name": "小明",
                        "entity_kind": "character",
                        "is_named": True,
                        "is_on_stage": True,
                        "evidence_refs": ["body:小明"],
                    }
                ],
            },
        )

        self.assertEqual(output.entity_mentions[0].entity_name, "小明")


if __name__ == "__main__":
    unittest.main()
