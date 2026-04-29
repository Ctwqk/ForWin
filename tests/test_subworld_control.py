from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sqlalchemy import select

from forwin.api_project_payloads import build_project_detail
from forwin.book_state import BookStateRepository
from forwin.checker.rules import ContinuityChecker
from forwin.context.assembler import assemble_context
from forwin.director.arc_director import ArcDirector
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.models.subworld import SubWorld, SubWorldRosterItem
from forwin.orchestrator.phase24 import ArcEnvelopeManager, ArcStructureDraftData
from forwin.orchestrator.phase3 import ReplanGovernor, StageAssessment
from forwin.protocol import (
    ArcPayoffMap,
    ChapterEntryTarget,
    ChapterExperiencePlan,
    EntityMention,
    ReaderPromise,
    ReviewVerdict,
    SubWorldPlanDelta,
    SubWorldSummary,
    WriterOutput,
)
from forwin.protocol.state_change import EventCandidate
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.subworld_manager import SubWorldManager
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.prompts import build_single_chapter_draft_prompt


class SubWorldControlTests(unittest.TestCase):
    def test_subworld_admission_ignores_unresolved_event_only_locations_or_items(self) -> None:
        class FakeRepo:
            def get_active_entities(self, _project_id: str) -> list[object]:
                return []

            def get_thread_by_name(self, _project_id: str, _name: str) -> object | None:
                return None

            def get_allowed_entity_names(self, _project_id: str, _chapter_number: int) -> set[str]:
                return {"林夜"}

            def get_entities_by_names(self, _project_id: str, _names: list[str]) -> dict[str, object]:
                return {}

        checker = ContinuityChecker(FakeRepo())
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=1,
                title="第一章",
                body="正文内容" * 80,
                end_of_chapter_summary="总结",
                new_events=[
                    EventCandidate(
                        summary="林夜在潮雾旧城发现锈蚀罗盘",
                        significance="major",
                        involved_entity_names=["林夜", "潮雾旧城", "锈蚀罗盘"],
                        roles=["protagonist", "location", "item"],
                    )
                ],
            ),
        )

        self.assertFalse(any(issue.rule_name == "sub_world_unknown_named_entity" for issue in verdict.issues))

    def test_ensure_registry_bootstraps_global_core_with_existing_characters(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = get_engine(postgres_test_url("subworld"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                entity = updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="阿青",
                    description="常驻角色",
                    chapter=0,
                )
                manager = SubWorldManager()

                global_core_id = manager.ensure_registry(session, project.id)

                global_core = session.get(SubWorld, global_core_id)
                roster = session.execute(
                    select(SubWorldRosterItem)
                    .where(SubWorldRosterItem.entity_id == entity.id)
                ).scalar_one_or_none()
            finally:
                session.close()
                engine.dispose()

        self.assertIsNotNone(global_core)
        self.assertEqual(global_core.scope, "global_core")
        self.assertIsNotNone(roster)
        self.assertEqual(roster.subworld_id, global_core_id)

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

    def test_assemble_context_uses_allowed_entities_from_active_subworlds(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = get_engine(postgres_test_url("context"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                arc = updater.create_arc_plan(project.id, "弧线")
                updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    one_line="开场",
                    goals=["推进"],
                )
                allowed = updater.create_entity(project.id, "character", "阿青", "允许角色", chapter=0)
                blocked = updater.create_entity(project.id, "character", "小明", "未激活角色", chapter=0)
                location = updater.create_entity(project.id, "location", "旧宅", "地点", chapter=0)
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
                    entity_id=allowed.id,
                    display_name="阿青",
                    description="允许角色",
                    is_core=True,
                    status="seeded_named",
                )
                updater.create_roster_item(
                    project_id=project.id,
                    subworld_id=arc_local.id,
                    entity_id=blocked.id,
                    display_name="小明",
                    description="未激活角色",
                    is_core=True,
                    status="seeded_named",
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
        self.assertIn("旧宅", active_names)
        self.assertNotIn("小明", active_names)
        self.assertEqual(context.allowed_entities, ["阿青"])
        self.assertEqual(context.entity_admission_rule, "strict_named_character")
        self.assertIn("Genesis 地区：主城核心区@主舞台总图·L1", context.genesis_map_overview)
        self.assertIn("运行时地区草案：江城前哨区@global_core·L1", context.genesis_map_overview)

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
            npc_intents=[],
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

    def test_continuity_checker_rejects_unknown_named_character(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = get_engine(postgres_test_url("checker"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                arc = updater.create_arc_plan(project.id, "弧线")
                updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    one_line="开场",
                    goals=["推进"],
                    experience_plan=ChapterExperiencePlan(entity_admission_rule="strict_named_character"),
                )
                allowed = updater.create_entity(project.id, "character", "阿青", "允许角色", chapter=0)
                global_core = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=arc.id,
                    parent_subworld_id=None,
                    name="global_core",
                    purpose="核心角色",
                    scope="global_core",
                    metadata={},
                )
                updater.create_roster_item(
                    project_id=project.id,
                    subworld_id=global_core.id,
                    entity_id=allowed.id,
                    display_name="阿青",
                    description="允许角色",
                    is_core=True,
                    status="seeded_named",
                )
                updater.update_chapter_experience_plan(
                    project.id,
                    1,
                    ChapterExperiencePlan(
                        active_subworld_ids=[global_core.id],
                        entity_admission_rule="strict_named_character",
                    ),
                )
                checker = ContinuityChecker(StateRepository(session))
                verdict = checker.check(
                    project.id,
                    WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body="正文内容" * 80,
                        end_of_chapter_summary="总结",
                        entity_mentions=[
                            EntityMention(
                                entity_name="小明",
                                entity_kind="character",
                                is_named=True,
                                is_on_stage=True,
                                evidence_refs=["body:小明出现"],
                            )
                        ],
                    ),
                )
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(verdict.verdict, "fail")
        self.assertTrue(any(issue.rule_name == "sub_world_unknown_named_entity" for issue in verdict.issues))

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

        with TemporaryDirectory() as tmp:
            engine = get_engine(postgres_test_url("rearc"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
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

    def test_phase24_persists_subworld_activation_into_band_and_chapter_plan(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = get_engine(postgres_test_url("phase24"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
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
                global_core_id = manager.ensure_registry(session, project.id)
                core_entity = updater.create_entity(project.id, "character", "阿青", "主角团成员", chapter=0)
                updater.create_roster_item(
                    project_id=project.id,
                    subworld_id=global_core_id,
                    entity_id=core_entity.id,
                    display_name="阿青",
                    description="主角团成员",
                    is_core=True,
                    status="seeded_named",
                )
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
        with TemporaryDirectory() as tmp:
            engine = get_engine(postgres_test_url("detail"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
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

    def test_planned_slot_materialization_writes_book_state_personality_and_roster_binding(self) -> None:
        engine = get_engine(postgres_test_url("planned-slot-personality"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project = updater.create_project(title="书", premise="p", genre="g")
            subworld = updater.create_subworld(
                project_id=project.id,
                origin_arc_id=None,
                parent_subworld_id=None,
                name="江城",
                purpose="本地线",
                scope="arc_local",
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

            entity = updater.materialize_roster_item(roster_item_id=roster.id, chapter=3)
            session.flush()
            session.refresh(roster)
            metadata = json.loads(roster.metadata_json or "{}")
            node = BookStateRepository(session).list_world_nodes(project.id)[0]
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(entity.kind, "character")
        self.assertEqual(metadata.get("character_id"), node.id)
        self.assertEqual(node.metadata.get("legacy_entity_id"), entity.id)
        self.assertEqual(node.profile["personality_loadout"]["dominant"]["skill"], "trait-loyal-protector")

    def test_subworld_core_named_character_uses_character_creation_helper(self) -> None:
        engine = get_engine(postgres_test_url("subworld-core-personality"))
        init_db(engine)
        session = get_session_factory(engine)()
        try:
            updater = StateUpdater(session)
            project = updater.create_project(title="书", premise="p", genre="g")
            arc = updater.create_arc_plan(project.id, "弧线")
            manager = SubWorldManager()
            manager.apply_arc_delta(
                session=session,
                updater=updater,
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
            node = BookStateRepository(session).list_world_nodes(project.id)[0]
            roster = session.execute(select(SubWorldRosterItem).where(SubWorldRosterItem.display_name == "沈临川")).scalar_one()
            roster_metadata = json.loads(roster.metadata_json or "{}")
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(node.name, "沈临川")
        self.assertEqual(node.profile["personality_loadout"]["dominant"]["skill"], "trait-loyal-protector")
        self.assertEqual(roster_metadata.get("character_id"), node.id)

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
