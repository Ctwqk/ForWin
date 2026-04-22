from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select

import forwin.api as api_module
from forwin.api_schemas import (
    BookGenesisPatchRequest,
    BookGenesisRefineRequest,
    BookGenesisNameGenerateRequest,
    BookGenesisStageRunRequest,
    ProjectCreateRequest,
)
from forwin.book_genesis import StaleGenesisRevisionError
from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.orchestrator.phase24 import ArcEnvelopeManager
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.updater import StateUpdater


class BookGenesisFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "genesis.db")
        engine = get_engine(self.db_path)
        init_db(engine)
        self.session_factory = get_session_factory(engine)
        self.engine = engine
        self.old_session_factory = api_module._SessionFactory
        self.old_config = api_module._config
        self.old_runtime_settings = api_module._runtime_settings
        api_module._SessionFactory = self.session_factory
        api_module._config = Config(
            db_path=self.db_path,
            minimax_api_key="test-key",
            minimax_base_url="http://example.invalid",
            minimax_model="fake-model",
        )
        api_module._runtime_settings = RuntimeSettingsStore(
            str(Path(self.tmpdir.name) / "runtime_settings.json"),
            default_api_key="default-key",
            default_base_url="http://default.invalid",
            default_model="default-model",
        )
        api_module._runtime_settings.save_profile(
            profile_id="genesis-alt",
            name="Genesis Alt",
            api_key="alt-key",
            base_url="http://alt.invalid",
            model="alt-model",
            set_as_default=False,
        )

    def tearDown(self) -> None:
        api_module._SessionFactory = self.old_session_factory
        api_module._config = self.old_config
        api_module._runtime_settings = self.old_runtime_settings
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_create_project_enters_creating_and_creates_initial_genesis_revision(self) -> None:
        response = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 测试书",
                    "premise": "主角在雨夜得到一枚会说话的镜子。",
                    "genre": "玄幻",
                    "target_total_chapters": 12,
                }
            )
        )

        with self.session_factory() as session:
            project = session.get(Project, response.project_id)
            revision = session.get(BookGenesisRevision, response.active_genesis_revision_id)

        assert project is not None
        assert revision is not None
        self.assertEqual(project.creation_status, "creating")
        self.assertEqual(project.active_genesis_revision_id, revision.id)
        self.assertEqual(revision.revision, 1)
        self.assertEqual(response.creation_status, "creating")
        self.assertTrue(response.workspace_url.endswith(f"project_id={project.id}"))

    def test_continue_generation_is_blocked_before_start_writing(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 阻断书",
                    "premise": "先创世，再写作。",
                    "genre": "都市",
                }
            )
        )

        with self.assertRaises(HTTPException) as exc:
            api_module.continue_project_generation(created.project_id, None)
        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("Genesis 阶段", str(exc.exception.detail))

    def test_initial_genesis_detail_contains_world_root_scaffold(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 根模型书",
                    "premise": "初始包要直接带完整 world 根骨架。",
                    "genre": "玄幻",
                }
            )
        )

        detail = api_module.get_project_genesis(created.project_id)
        world = detail.pack.world

        self.assertIn("world_bible", world)
        self.assertIn("map_atlas", world)
        self.assertIn("story_engine", world)
        self.assertIn("minimum_world_system", world)
        self.assertIn("minimum_extension_pack", world)
        self.assertIn("institution_profiles", world)
        self.assertIn("resource_economy_profiles", world)
        self.assertIn("world_extensions", world)
        self.assertIn("template_libraries", world)
        self.assertIn("daily_life_profiles", world["world_extensions"])
        self.assertIn("title_lexicon_pack", world["template_libraries"])

    def test_start_writing_materializes_arc_skeletons_and_active_arc_chapters(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 启动书",
                    "premise": "一座旧城里复苏的禁术会把所有人拖入更深的债。",
                    "genre": "玄幻",
                    "target_total_chapters": 6,
                }
            )
        )

        api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "world_bible": {"overview": "旧城与禁术并存的世界。"},
                        "map_atlas": {"overview": "旧城、城外荒原、地下遗迹。"},
                        "story_engine": {"long_arcs": ["旧术复苏", "主角代价升级"]},
                    },
                    "book_arc_blueprint": {
                        "summary": "两段式蓝图",
                        "arcs": [
                            {
                                "arc_number": 1,
                                "title": "旧城开局",
                                "arc_synopsis": "主角被迫卷入旧城禁术的第一轮冲突。",
                                "goal": "立主冲突",
                                "stakes": "会失去立足点与关键同伴",
                                "payoff_direction": "局部揭秘",
                                "chapter_start": 1,
                                "chapter_end": 3,
                                "chapter_count": 3,
                                "target_size": 3,
                                "soft_min": 2,
                                "soft_max": 4,
                            },
                            {
                                "arc_number": 2,
                                "title": "遗迹升级",
                                "arc_synopsis": "冲突延伸到城外遗迹并拉高世界代价。",
                                "goal": "升级世界冲突",
                                "stakes": "旧术外泄导致更大灾难",
                                "payoff_direction": "更大悬念",
                                "chapter_start": 4,
                                "chapter_end": 6,
                                "chapter_count": 3,
                                "target_size": 3,
                                "soft_min": 2,
                                "soft_max": 4,
                            },
                        ],
                    },
                    "execution_bootstrap": {"operation_mode": "blackbox", "root_ready": True},
                }
            ),
        )

        for stage_key in ("brief", "world", "map", "story_engine", "book_blueprint", "bootstrap"):
            api_module.lock_project_genesis_stage(created.project_id, stage_key)

        def fake_genesis_call(self, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            if str(stage_key).startswith("launch_arc_"):
                return (
                    {
                        "chapters": [
                            {
                                "title": "雨夜开端",
                                "one_line": "主角在雨夜第一次正面撞上禁术代价。",
                                "goals": ["建立危机", "种下旧术线索"],
                            },
                            {
                                "title": "债务浮现",
                                "one_line": "旧城势力开始围拢主角，代价被迫升级。",
                                "goals": ["扩大冲突", "抬高关系风险"],
                            },
                            {
                                "title": "遗迹坐标",
                                "one_line": "主角得到通往下一 Arc 的遗迹入口。",
                                "goals": ["给出阶段性揭秘", "推到下一段世界舞台"],
                            },
                        ]
                    },
                    {
                        "effective_system_prompt": "launch arc planner",
                        "prompt_layers": [{"role": "system", "content": "launch arc planner"}],
                        "input_snapshot": {"stage_key": stage_key},
                        "model_profile": {"model": "fake-model"},
                        "attempts": [{"attempt": 1, "status": "success"}],
                        "output_summary": {"mode": "success"},
                    },
                )
            return fallback, {
                "effective_system_prompt": "fallback",
                "prompt_layers": [],
                "input_snapshot": {},
                "model_profile": {"model": "fake-model"},
                "attempts": [{"attempt": 1, "status": "fallback"}],
                "output_summary": {"mode": "fallback"},
            }

        with (
            patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_genesis_call),
            patch("forwin.api._create_continue_generation_task", return_value="task-genesis-001"),
        ):
            response = api_module.start_project_writing(created.project_id)

        with self.session_factory() as session:
            project = session.get(Project, created.project_id)
            arcs = session.execute(
                select(ArcPlanVersion)
                .where(ArcPlanVersion.project_id == created.project_id)
                .order_by(ArcPlanVersion.arc_number.asc())
            ).scalars().all()
            plans = session.execute(
                select(ChapterPlan)
                .where(ChapterPlan.project_id == created.project_id)
                .order_by(ChapterPlan.chapter_number.asc())
            ).scalars().all()
            traces = session.execute(
                select(PromptTrace)
                .where(PromptTrace.project_id == created.project_id)
                .order_by(PromptTrace.created_at.asc())
            ).scalars().all()

        assert project is not None
        self.assertEqual(response.task_id, "task-genesis-001")
        self.assertEqual(project.creation_status, "writing")
        self.assertEqual(len(arcs), 2)
        self.assertEqual(arcs[0].status, "active")
        self.assertEqual(arcs[0].planned_target_size, 3)
        self.assertEqual(arcs[1].status, "planned")
        self.assertEqual([plan.chapter_number for plan in plans], [1, 2, 3])
        self.assertTrue(any(trace.trace_scope == "start_writing" for trace in traces))

    def test_arc_envelope_uses_genesis_persisted_arc_sizing(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis Sizing 书",
                    "premise": "先锁定根蓝图，再启动写作。",
                    "genre": "玄幻",
                    "target_total_chapters": 30,
                }
            )
        )

        api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "world_bible": {"overview": "世界规则已锁定。"},
                        "map_atlas": {"overview": "一城一野一遗迹。"},
                        "story_engine": {"long_arcs": ["根冲突", "外部升级"]},
                    },
                    "book_arc_blueprint": {
                        "summary": "单 arc 启动蓝图",
                        "arcs": [
                            {
                                "arc_number": 1,
                                "title": "开篇长弧",
                                "arc_synopsis": "主角被卷入根冲突并逐步看见世界代价。",
                                "goal": "建立根冲突",
                                "stakes": "失去立足点",
                                "payoff_direction": "局部揭秘",
                                "chapter_start": 1,
                                "chapter_end": 5,
                                "chapter_count": 5,
                                "target_size": 9,
                                "soft_min": 7,
                                "soft_max": 11,
                            }
                        ],
                    },
                    "execution_bootstrap": {"operation_mode": "blackbox", "root_ready": True},
                }
            ),
        )

        for stage_key in ("brief", "world", "map", "story_engine", "book_blueprint", "bootstrap"):
            api_module.lock_project_genesis_stage(created.project_id, stage_key)

        def fake_genesis_call(self, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            if str(stage_key).startswith("launch_arc_"):
                return (
                    {
                        "chapters": [
                            {"title": "第1章", "one_line": "起势", "goals": ["推进"]},
                            {"title": "第2章", "one_line": "升级", "goals": ["推进"]},
                            {"title": "第3章", "one_line": "加压", "goals": ["推进"]},
                            {"title": "第4章", "one_line": "转折", "goals": ["推进"]},
                            {"title": "第5章", "one_line": "收束", "goals": ["推进"]},
                        ]
                    },
                    {
                        "effective_system_prompt": "launch arc planner",
                        "prompt_layers": [{"role": "system", "content": "launch arc planner"}],
                        "input_snapshot": {"stage_key": stage_key},
                        "model_profile": {"model": "fake-model"},
                        "attempts": [{"attempt": 1, "status": "success"}],
                        "output_summary": {"mode": "success"},
                    },
                )
            return fallback, {
                "effective_system_prompt": "fallback",
                "prompt_layers": [],
                "input_snapshot": {},
                "model_profile": {"model": "fake-model"},
                "attempts": [{"attempt": 1, "status": "fallback"}],
                "output_summary": {"mode": "fallback"},
            }

        with (
            patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_genesis_call),
            patch("forwin.api._create_continue_generation_task", return_value="task-genesis-size-001"),
        ):
            api_module.start_project_writing(created.project_id)

        with self.session_factory() as session:
            project = session.get(Project, created.project_id)
            active_arc = session.execute(
                select(ArcPlanVersion)
                .where(ArcPlanVersion.project_id == created.project_id)
                .where(ArcPlanVersion.status == "active")
            ).scalar_one()
            manager = ArcEnvelopeManager(director=None)
            envelope = manager.ensure_active_arc_resolution(
                session=session,
                project_id=created.project_id,
                activation_chapter=1,
            )

        assert project is not None
        self.assertEqual(project.creation_status, "writing")
        self.assertEqual(active_arc.planned_target_size, 9)
        self.assertEqual(active_arc.planned_soft_min, 7)
        self.assertEqual(active_arc.planned_soft_max, 11)
        self.assertEqual(envelope.base_target_size, 9)
        self.assertEqual(envelope.base_soft_min, 7)
        self.assertEqual(envelope.base_soft_max, 11)

    def test_refine_stage_updates_stage_json_and_records_instruction_trace(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 对话改写书",
                    "premise": "需要对话式改写 story engine。",
                    "genre": "玄幻",
                }
            )
        )

        api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "story_engine": {
                            "core_cast": [
                                {"name": "林昭", "role": "主角", "desire": "活下去", "fear": "拖累家人"}
                            ],
                            "factions": [],
                            "opposition": [],
                            "relationship_axes": ["林昭与旧秩序"],
                            "reader_promises": ["持续升级"],
                            "long_arcs": ["成长"],
                        }
                    }
                }
            ),
        )

        test_case = self

        def fake_refine_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            test_case.assertEqual(stage_key, "story_engine:refine")
            test_case.assertIn("更阴郁一点", messages[1]["content"])
            return (
                {
                    "core_cast": [
                        {"name": "林昭", "role": "主角", "desire": "活下去", "fear": "更阴郁地害怕拖累家人", "secret": "对旧秩序有复杂阴影"}
                    ],
                    "factions": [
                        {"name": "城防司", "role": "秩序势力", "goal": "控制城内异动", "leverage": "武力与法统"}
                    ],
                    "opposition": [],
                    "relationship_axes": ["林昭与旧秩序"],
                    "reader_promises": ["持续升级"],
                    "long_arcs": ["成长"],
                },
                {
                    "effective_system_prompt": "genesis refine",
                    "prompt_layers": [{"role": "system", "content": "genesis refine"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fake-model"},
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_refine_call):
            detail = api_module.refine_project_genesis_stage(
                created.project_id,
                "story_engine",
                BookGenesisRefineRequest.model_validate(
                    {
                        "instruction": "把主角写得更阴郁一点，并补一个秩序型势力。",
                        "reason": "test_refine_story_engine",
                    }
                ),
            )

        self.assertEqual(detail.pack.world["story_engine"]["core_cast"][0]["fear"], "更阴郁地害怕拖累家人")
        self.assertEqual(detail.pack.world["story_engine"]["core_cast"][0]["secret"], "对旧秩序有复杂阴影")
        self.assertEqual(detail.pack.world["story_engine"]["factions"][0]["name"], "城防司")

        with self.session_factory() as session:
            traces = session.execute(
                select(PromptTrace)
                .where(PromptTrace.project_id == created.project_id)
                .order_by(PromptTrace.created_at.desc())
            ).scalars().all()

        self.assertTrue(traces)
        self.assertEqual(traces[0].trace_scope, "genesis_refine")
        self.assertEqual(traces[0].stage_key, "story_engine")
        self.assertIn("更阴郁一点", traces[0].input_snapshot_json)

    def test_generate_stage_uses_selected_model_profile(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 模型选择书",
                    "premise": "需要给 Genesis 生成动作显式选模型。",
                    "genre": "玄幻",
                }
            )
        )

        test_case = self

        def fake_generate_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            test_case.assertEqual(stage_key, "world")
            test_case.assertEqual(getattr(_service.llm_client, "profile_id", ""), "genesis-alt")
            test_case.assertEqual(getattr(_service.llm_client, "profile_name", ""), "Genesis Alt")
            test_case.assertEqual(getattr(_service.llm_client, "model", ""), "alt-model")
            test_case.assertEqual(getattr(_service.llm_client, "base_url", ""), "http://alt.invalid")
            return (
                {
                    "overview": "被选中模型生成的世界观。",
                    "axioms": ["规则 1"],
                    "history_slice": "历史片段",
                    "naming_style": "简洁",
                    "forbidden_zones": [],
                },
                {
                    "effective_system_prompt": "genesis world",
                    "prompt_layers": [{"role": "system", "content": "genesis world"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {
                        "profile_id": "genesis-alt",
                        "profile_name": "Genesis Alt",
                        "model": "alt-model",
                        "base_url": "http://alt.invalid",
                    },
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_generate_call):
            detail = api_module.generate_project_genesis_stage(
                created.project_id,
                "world",
                BookGenesisStageRunRequest.model_validate({"model_profile_id": "genesis-alt"}),
            )

        self.assertEqual(detail.pack.world["world_bible"]["overview"], "被选中模型生成的世界观。")

        with self.session_factory() as session:
            trace = session.execute(
                select(PromptTrace)
                .where(PromptTrace.project_id == created.project_id)
                .order_by(PromptTrace.created_at.desc())
            ).scalars().first()

        assert trace is not None
        self.assertIn('"profile_id": "genesis-alt"', trace.model_profile_json)
        self.assertIn('"profile_name": "Genesis Alt"', trace.model_profile_json)

    def test_map_and_story_fallbacks_include_subworld_metadata_and_links(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 子世界联动书",
                    "premise": "主角的身世与故乡、宗派、城池一起牵动整本书的矛盾扩张。",
                    "genre": "玄幻",
                    "target_total_chapters": 18,
                }
            )
        )

        def fake_fallback_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            return (
                fallback,
                {
                    "effective_system_prompt": f"genesis {stage_key}",
                    "prompt_layers": [{"role": "system", "content": f"genesis {stage_key}"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fallback-model"},
                    "attempts": [{"attempt": 1, "status": "fallback"}],
                    "output_summary": {"mode": "fallback"},
                },
            )

        with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_fallback_call):
            api_module.generate_project_genesis_stage(
                created.project_id,
                "world",
                BookGenesisStageRunRequest.model_validate({}),
            )
            api_module.generate_project_genesis_stage(
                created.project_id,
                "map",
                BookGenesisStageRunRequest.model_validate({}),
            )
            detail = api_module.generate_project_genesis_stage(
                created.project_id,
                "story_engine",
                BookGenesisStageRunRequest.model_validate({}),
            )

        culture_profile = detail.pack.world["world_bible"]["culture_profiles"][0]
        submap = detail.pack.world["map_atlas"]["submaps"][0]
        region = detail.pack.world["map_atlas"]["regions"][0]
        child_region = detail.pack.world["map_atlas"]["regions"][1]
        node = detail.pack.world["map_atlas"]["nodes"][0]
        protagonist = detail.pack.world["story_engine"]["core_cast"][0]
        faction = detail.pack.world["story_engine"]["factions"][0]
        opposition = detail.pack.world["story_engine"]["opposition"][0]

        self.assertEqual(culture_profile["id"], "culture-main-stage")
        self.assertTrue(culture_profile["character_name_examples"])
        self.assertTrue(culture_profile["region_name_examples"])
        self.assertTrue(culture_profile["location_name_examples"])
        self.assertEqual(submap["culture_profile_id"], culture_profile["id"])
        self.assertIn("culture_traits", submap)
        self.assertIn("climate", submap)
        self.assertIn("terrain", submap)
        self.assertIn("governing_power", submap)
        self.assertIn("resident_factions", submap)
        self.assertEqual(region["level"], 1)
        self.assertEqual(region["culture_profile_id"], culture_profile["id"])
        self.assertEqual(child_region["level"], 2)
        self.assertEqual(child_region["parent_region_id"], region["id"])
        self.assertEqual(child_region["culture_profile_id"], culture_profile["id"])
        self.assertEqual(submap["id"], "subworld-main-stage")
        self.assertEqual(node["id"], "node-main-stage")
        self.assertEqual(node["parent_subworld"], submap["id"])
        self.assertEqual(node["parent_region_id"], region["id"])
        self.assertEqual(node["culture_profile_id"], culture_profile["id"])
        self.assertEqual(protagonist["culture_profile_id"], culture_profile["id"])
        self.assertEqual(protagonist["home_subworld"], submap["id"])
        self.assertEqual(protagonist["home_region"], region["id"])
        self.assertEqual(protagonist["home_location"], node["id"])
        self.assertEqual(protagonist["current_region"], region["id"])
        self.assertEqual(protagonist["current_base"], node["id"])
        self.assertEqual(faction["id"], "faction-main-stage")
        self.assertEqual(protagonist["affiliated_faction"], faction["id"])
        self.assertEqual(len([item for item in protagonist["faction_memberships"] if item["is_primary"]]), 1)
        self.assertEqual(faction["culture_profile_id"], culture_profile["id"])
        self.assertEqual(faction["base_subworld"], submap["id"])
        self.assertEqual(faction["headquarters_region"], child_region["id"])
        self.assertEqual(faction["footprint"][0]["region_id"], region["id"])
        self.assertEqual(opposition["culture_profile_id"], culture_profile["id"])
        self.assertEqual(opposition["backing_faction"], faction["id"])
        self.assertEqual(opposition["base_region"], child_region["id"])
        self.assertEqual(opposition["backing_factions"], [faction["id"]])

    def test_refine_stage_target_path_only_updates_selected_item(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 子项改写书",
                    "premise": "需要对子项做定向改写。",
                    "genre": "玄幻",
                }
            )
        )

        api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "map_atlas": {
                            "overview": "结构化地图",
                            "topology_rules": ["地点之间有风险与成本"],
                            "submaps": [
                                {
                                    "name": "北城区",
                                    "scope": "district",
                                    "parent_scope": "主城",
                                    "summary": "旧工业区",
                                    "key_locations": ["焚化塔"],
                                    "travel_rules": ["夜间封锁"],
                                },
                                {
                                    "name": "南城区",
                                    "scope": "district",
                                    "parent_scope": "主城",
                                    "summary": "商业区",
                                    "key_locations": ["码头"],
                                    "travel_rules": ["白天繁忙"],
                                },
                            ],
                            "nodes": [],
                            "edges": [],
                        }
                    }
                }
            ),
        )

        test_case = self

        def fake_refine_item_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            test_case.assertEqual(stage_key, "map:refine_item")
            test_case.assertIn("submaps[0]", messages[1]["content"])
            return (
                {
                    "name": "北城区",
                    "scope": "district",
                    "parent_scope": "主城",
                    "summary": "旧工业区，空气里常年弥漫焦灰味。",
                    "key_locations": ["焚化塔", "封存轨道站"],
                    "travel_rules": ["夜间封锁", "暴雨时禁行"],
                },
                {
                    "effective_system_prompt": "genesis refine item",
                    "prompt_layers": [{"role": "system", "content": "genesis refine item"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fake-model"},
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_refine_item_call):
            detail = api_module.refine_project_genesis_stage(
                created.project_id,
                "map",
                BookGenesisRefineRequest.model_validate(
                    {
                        "instruction": "把这个小地图写得更有末日工业感，并补一个关键地点。",
                        "target_path": "submaps[0]",
                        "reason": "test_refine_map_item",
                    }
                ),
            )

        self.assertEqual(detail.pack.world["map_atlas"]["submaps"][0]["key_locations"], ["焚化塔", "封存轨道站"])
        self.assertEqual(detail.pack.world["map_atlas"]["submaps"][1]["summary"], "商业区")

    def test_refine_stage_target_path_can_update_scalar_world_field(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 世界观字段改写书",
                    "premise": "需要把历史切片单独对话改写。",
                    "genre": "玄幻",
                }
            )
        )

        api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "world_bible": {
                            "overview": "旧秩序松动中的长篇世界。",
                            "axioms": ["力量与代价绑定"],
                            "history_slice": "旧王朝崩塌后的百年乱局尚未结束。",
                            "naming_style": "中文网文风格，短促有力。",
                            "forbidden_zones": ["不要用现代网络梗"],
                        }
                    }
                }
            ),
        )

        test_case = self

        def fake_refine_scalar_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            test_case.assertEqual(stage_key, "world:refine_item")
            test_case.assertIn("history_slice", messages[1]["content"])
            return (
                {"value": "旧王朝崩塌后的百年乱局进入第二次秩序重组前夜。"},
                {
                    "effective_system_prompt": "genesis refine scalar item",
                    "prompt_layers": [{"role": "system", "content": "genesis refine scalar item"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fake-model"},
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_refine_scalar_call):
            detail = api_module.refine_project_genesis_stage(
                created.project_id,
                "world",
                BookGenesisRefineRequest.model_validate(
                    {
                        "instruction": "把历史切片改得更像旧王朝崩塌后的二次重组前夜。",
                        "target_path": "history_slice",
                        "reason": "test_refine_world_history_slice",
                    }
                ),
            )

        self.assertEqual(detail.pack.world["world_bible"]["history_slice"], "旧王朝崩塌后的百年乱局进入第二次秩序重组前夜。")
        self.assertEqual(detail.pack.world["world_bible"]["naming_style"], "中文网文风格，短促有力。")

    def test_stale_genesis_revision_cannot_overwrite_newer_revision(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 并发保护书",
                    "premise": "旧 revision 不能覆盖新 revision。",
                    "genre": "玄幻",
                }
            )
        )

        stale_revision_id = created.active_genesis_revision_id
        api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "story_engine": {
                            "core_cast": [{"name": "主角", "role": "主视角"}],
                            "factions": [],
                            "opposition": [],
                            "relationship_axes": [],
                            "reader_promises": [],
                            "long_arcs": [],
                        }
                    }
                }
            ),
        )

        with self.session_factory() as session:
            project = session.get(Project, created.project_id)
            stale_revision = session.get(BookGenesisRevision, stale_revision_id)
            assert project is not None
            assert stale_revision is not None
            updater = StateUpdater(session)
            service = api_module._build_genesis_service()
            try:
                with self.assertRaises(StaleGenesisRevisionError):
                    service.patch_pack(
                        session=session,
                        updater=updater,
                        project=project,
                        revision=stale_revision,
                        patch={"world": {"story_engine": {"core_cast": [{"name": "旧角色", "role": "过期写入"}]}}},
                        reason="stale_write",
                    )
            finally:
                api_module._close_genesis_service(service)

    def test_generate_project_genesis_name_uses_culture_profile_generator(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "命名引擎测试书",
                    "premise": "不同文明风格需要可复用的命名生成。",
                    "genre": "玄幻",
                    "target_total_chapters": 12,
                }
            )
        )

        api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "world_bible": {
                            "overview": "多文明并存的长篇舞台。",
                            "culture_profiles": [
                                {
                                    "id": "culture-sinic",
                                    "name": "中华风",
                                    "summary": "偏礼制、旧城、宗门气息。",
                                    "inspiration": "中华",
                                    "generator_civilization": "中华",
                                    "generator_overlays": ["基督教"],
                                    "character_name_examples": [],
                                    "region_name_examples": [],
                                    "location_name_examples": [],
                                }
                            ],
                        }
                    }
                }
            ),
        )

        response = api_module.generate_project_genesis_name(
            created.project_id,
            BookGenesisNameGenerateRequest.model_validate(
                {
                    "stage_key": "world",
                    "target_path": "world_bible.culture_profiles[0]",
                    "field_path": "character_name_examples",
                    "kind": "person",
                    "count": 4,
                    "nonce": "test-001",
                }
            ),
        )

        self.assertEqual(response.stage_key, "world")
        self.assertEqual(response.kind, "person")
        self.assertEqual(response.culture_profile_id, "culture-sinic")
        self.assertEqual(response.generator_civilization, "中华+基督教")
        self.assertEqual(len(response.suggestions), 4)
        self.assertEqual(response.applied_value, response.suggestions)
        self.assertTrue(all(isinstance(item, str) and item.strip() for item in response.suggestions))

    def test_patch_world_auto_fills_missing_submap_node_and_faction_ids(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis ID 自动补齐书",
                    "premise": "手工 PATCH 也应该补齐稳定 ID。",
                    "genre": "玄幻",
                }
            )
        )

        detail = api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "map_atlas": {
                            "overview": "旧城地图",
                            "topology_rules": ["移动有代价"],
                            "submaps": [{"name": "主舞台", "scope": "macro_region"}],
                            "regions": [{"name": "主城", "subworld_name": "主舞台", "level": 1}],
                            "nodes": [{"name": "旧城", "parent_subworld": "主舞台", "parent_region_id": "region-1"}],
                            "edges": [],
                        },
                        "story_engine": {
                            "core_cast": [],
                            "factions": [{"name": "城防司", "goal": "维持秩序"}],
                            "opposition": [],
                            "relationship_axes": [],
                            "reader_promises": [],
                            "long_arcs": [],
                        },
                    }
                }
            ),
        )

        submap = detail.pack.world["map_atlas"]["submaps"][0]
        node = detail.pack.world["map_atlas"]["nodes"][0]
        faction = detail.pack.world["story_engine"]["factions"][0]

        self.assertTrue(submap["id"])
        self.assertTrue(node["id"])
        self.assertTrue(faction["id"])
        self.assertEqual(node["parent_subworld"], submap["id"])

    def test_legacy_top_level_genesis_sections_upgrade_on_read_and_write_back_as_world_root(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 旧版兼容书",
                    "premise": "旧 revision 要自动升级到统一 world 根。",
                    "genre": "玄幻",
                }
            )
        )

        with self.session_factory() as session:
            project = session.get(Project, created.project_id)
            revision = session.get(BookGenesisRevision, created.active_genesis_revision_id)
            assert project is not None
            assert revision is not None
            revision.pack_json = json.dumps(
                {
                    "book_brief": {"title": "旧版包"},
                    "world_bible": {"overview": "旧格式世界观"},
                    "map_atlas": {"overview": "旧格式地图"},
                    "story_engine": {"long_arcs": ["旧格式引擎"]},
                    "book_arc_blueprint": {},
                    "subworld_policy": {},
                    "execution_bootstrap": {},
                    "stage_states": {},
                },
                ensure_ascii=False,
            )
            session.add(revision)
            session.commit()

        detail = api_module.get_project_genesis(created.project_id)
        self.assertEqual(detail.pack.world["world_bible"]["overview"], "旧格式世界观")
        self.assertEqual(detail.pack.world["map_atlas"]["overview"], "旧格式地图")
        self.assertEqual(detail.pack.world["story_engine"]["long_arcs"], ["旧格式引擎"])
        self.assertIn("minimum_world_system", detail.pack.world)

        detail = api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {"world": {"world_bible": {"history_slice": "升级后新写回"}}}
            ),
        )
        self.assertEqual(detail.pack.world["world_bible"]["history_slice"], "升级后新写回")

        with self.session_factory() as session:
            project = session.get(Project, created.project_id)
            assert project is not None
            latest_revision = session.get(BookGenesisRevision, project.active_genesis_revision_id)
            assert latest_revision is not None
            saved_payload = json.loads(latest_revision.pack_json or "{}")

        self.assertIn("world", saved_payload)
        self.assertNotIn("world_bible", saved_payload)
        self.assertNotIn("map_atlas", saved_payload)
        self.assertNotIn("story_engine", saved_payload)


if __name__ == "__main__":
    unittest.main()
