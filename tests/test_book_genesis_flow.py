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
from forwin.book_genesis import BookGenesisService
from forwin.book_genesis import StaleGenesisRevisionError
from forwin.book_genesis import _fallback_brief
from forwin.book_genesis import _fallback_blueprint
from forwin.book_genesis import _fallback_map
from forwin.book_genesis import _fallback_named_entity_seed
from forwin.config import Config
from forwin.governance import DecisionEventType
from forwin.map.models import MapEdgeRow, MapGenerationRunRow, MapNodeRow, MapRegionRow
from forwin.map.protocol import BookMapGenerationResult, MapValidationReport
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.governance import DecisionEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.orchestrator.phase24 import ArcEnvelopeManager
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.skills import build_skill_runtime_components
from forwin.state.updater import StateUpdater


class BookGenesisFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.database_url = postgres_test_url("genesis")
        engine = get_engine(self.database_url)
        init_db(engine)
        self.session_factory = get_session_factory(engine)
        self.engine = engine
        self.old_session_factory = api_module._SessionFactory
        self.old_config = api_module._config
        self.old_runtime_settings = api_module._runtime_settings
        api_module._SessionFactory = self.session_factory
        api_module._config = Config(
            database_url=self.database_url,
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

    def test_fallback_map_does_not_promote_placeholder_guardrails_to_places(self) -> None:
        pack = {
            "book_brief": {
                "premise": "主角：林澈。质量要求：不要使用“相关人员”等正文占位符。",
                "setting_seed": "地下旧轨、潮汐钟楼、岫苑、档案公会、失忆广场是核心场景。",
            },
            "world": {
                "world_bible": {
                    "overview": "旧城由白塔记忆系统维持公共档案秩序。",
                    "culture_profiles": [{"id": "culture-main-stage", "generator_civilization": "中华"}],
                }
            },
        }

        seed = _fallback_named_entity_seed(pack)
        fallback_map = _fallback_map(pack)
        serialized = json.dumps({"seed": seed, "map": fallback_map}, ensure_ascii=False)

        self.assertIn("林澈", seed["characters"])
        for name in ("地下旧轨", "潮汐钟楼", "岫苑", "档案公会", "失忆广场"):
            self.assertIn(name, serialized)
        self.assertNotIn("相关人员", serialized)

    def test_fallback_brief_prefers_story_conflict_over_test_metadata(self) -> None:
        project = Project(
            title="旧城遗档",
            genre="悬疑科幻",
            premise=(
                "本书用于 ForWin 新 canon quality gate 真实端到端复测。\n"
                "主角：林澈，旧城档案修复师。\n"
                "背景：一座每隔十年会遗失一段历史的城市。\n"
                "核心冲突：林澈必须在记忆重置前找回家族档案，并决定是否公开白塔真相。\n"
                "质量要求：不要使用正文占位符。"
            ),
            setting_summary="旧城由白塔记忆系统维持公共档案秩序。",
            target_total_chapters=60,
        )

        brief = _fallback_brief(project, {})

        self.assertIn("林澈必须在记忆重置前", brief["one_line"])
        self.assertNotIn("长篇长篇", brief["one_line"])
        self.assertNotIn("ForWin", brief["one_line"])
        self.assertNotIn("质量要求", brief["one_line"])

    def test_fallback_blueprint_uses_clean_brief_focus_for_arc_synopsis(self) -> None:
        project = Project(
            title="旧城遗档",
            genre="悬疑科幻",
            premise=(
                "本书用于 ForWin 新 canon quality gate 真实端到端复测。\n"
                "主角：林澈，旧城档案修复师。\n"
                "背景：一座每隔十年会遗失一段历史的城市。\n"
                "核心冲突：林澈必须在记忆重置前找回家族档案，并决定是否公开白塔真相。\n"
                "质量要求：不要使用正文占位符。"
            ),
            setting_summary="旧城由白塔记忆系统维持公共档案秩序。",
            target_total_chapters=60,
        )
        pack = {"book_brief": _fallback_brief(project, {})}

        blueprint = _fallback_blueprint(project, pack)
        serialized = json.dumps(blueprint, ensure_ascii=False)

        self.assertIn("林澈必须在记忆重置前", serialized)
        self.assertNotIn("悬疑科幻长篇，围绕", serialized)
        self.assertNotIn("展开”推进", serialized)
        self.assertNotIn("ForWin", serialized)
        self.assertNotIn("质量要求", serialized)
        self.assertNotIn("主角：", serialized)

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

    def test_patching_map_after_world_lock_keeps_world_stage_locked(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 锁定保持书",
                    "premise": "世界观锁定后，地图阶段继续细化不应把世界观重新解锁。",
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
                            "overview": "旧城、荒原与遗迹并存。",
                            "axioms": ["力量必须付出代价"],
                            "history_slice": "旧王朝崩塌后的乱局时代。",
                            "naming_style": "中文短促命名。",
                            "forbidden_zones": ["避免现代网络梗"],
                        },
                        "map_atlas": {
                            "overview": "初始地图",
                            "topology_rules": ["行动必须有路程与风险成本"],
                            "submaps": [],
                            "regions": [],
                            "nodes": [],
                            "edges": [],
                        },
                    }
                }
            ),
        )
        api_module.lock_project_genesis_stage(created.project_id, "world")

        detail = api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "world": {
                        "map_atlas": {
                            "overview": "细化后的地图",
                            "topology_rules": ["行动必须有路程与风险成本", "跨区移动要付出公开代价"],
                            "submaps": [],
                            "regions": [],
                            "nodes": [],
                            "edges": [],
                        }
                    },
                    "reason": "test_patch_map_keeps_world_locked",
                }
            ),
        )

        self.assertTrue(detail.pack.stage_states["world"].locked)
        self.assertEqual(detail.pack.stage_states["world"].status, "locked")
        self.assertFalse(detail.pack.stage_states["map"].locked)
        self.assertEqual(detail.pack.stage_states["map"].status, "edited")
        self.assertEqual(detail.pack.world["map_atlas"]["overview"], "细化后的地图")

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
            map_runs = session.execute(
                select(MapGenerationRunRow).where(MapGenerationRunRow.project_id == created.project_id)
            ).scalars().all()
            map_region_count = session.execute(
                select(MapRegionRow).where(MapRegionRow.project_id == created.project_id)
            ).scalars().all()
            map_node_count = session.execute(
                select(MapNodeRow).where(MapNodeRow.project_id == created.project_id)
            ).scalars().all()
            map_edge_count = session.execute(
                select(MapEdgeRow).where(MapEdgeRow.project_id == created.project_id)
            ).scalars().all()
            decision_events = session.execute(
                select(DecisionEvent).where(DecisionEvent.project_id == created.project_id)
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
        self.assertTrue(map_runs)
        self.assertTrue(map_region_count)
        self.assertTrue(map_node_count)
        self.assertTrue(map_edge_count)
        self.assertTrue(
            any(event.event_type == DecisionEventType.MAP_GENERATION_SUCCEEDED for event in decision_events)
        )

    def test_start_writing_blocks_when_initial_book_map_generation_fails(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 地图失败书",
                    "premise": "启动写作前地图生成失败时不能进入写作。",
                    "genre": "玄幻",
                    "target_total_chapters": 3,
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
                        "story_engine": {"long_arcs": ["旧术复苏"]},
                    },
                    "book_arc_blueprint": {
                        "summary": "单 arc 蓝图",
                        "arcs": [
                            {
                                "arc_number": 1,
                                "title": "旧城开局",
                                "arc_synopsis": "主角被迫卷入旧城禁术。",
                                "goal": "立主冲突",
                                "stakes": "失去立足点",
                                "payoff_direction": "局部揭秘",
                                "chapter_start": 1,
                                "chapter_end": 3,
                                "chapter_count": 3,
                                "target_size": 3,
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
                            {"title": "雨夜", "one_line": "主角进入旧城。", "goals": ["建立危机"]},
                            {"title": "债务", "one_line": "势力围拢。", "goals": ["扩大冲突"]},
                            {"title": "遗迹", "one_line": "得到遗迹坐标。", "goals": ["转入下一阶段"]},
                        ]
                    },
                    {
                        "effective_system_prompt": "launch arc planner",
                        "prompt_layers": [],
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

        invalid_map = BookMapGenerationResult(
            project_id=created.project_id,
            validation_report=MapValidationReport(valid=False, errors=["bad map"]),
        )
        with (
            patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_genesis_call),
            patch("forwin.genesis_handoff.map_bootstrap.create_or_update_book_map", return_value=invalid_map),
            patch("forwin.api._create_continue_generation_task") as task_mock,
        ):
            with self.assertRaises(HTTPException) as raised:
                api_module.start_project_writing(created.project_id)

        self.assertEqual(raised.exception.status_code, 409)
        task_mock.assert_not_called()
        with self.session_factory() as session:
            project = session.get(Project, created.project_id)
            plans = session.execute(
                select(ChapterPlan).where(ChapterPlan.project_id == created.project_id)
            ).scalars().all()
            events = session.execute(
                select(DecisionEvent).where(DecisionEvent.project_id == created.project_id)
            ).scalars().all()

        assert project is not None
        self.assertEqual(project.creation_status, "genesis_ready")
        self.assertEqual(plans, [])
        self.assertTrue(any(event.event_type == DecisionEventType.MAP_GENERATION_FAILED for event in events))

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
            system_prompt = "\n".join(item["content"] for item in messages if item["role"] == "system")
            user_prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            test_case.assertEqual(stage_key, "story_engine:refine")
            test_case.assertIn("优先局部改动", system_prompt)
            test_case.assertIn("【用户指令】", user_prompt)
            test_case.assertIn("更阴郁一点", user_prompt)
            test_case.assertIn("【阶段硬约束】", user_prompt)
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
            system_prompt = "\n".join(item["content"] for item in messages if item["role"] == "system")
            user_prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            test_case.assertEqual(stage_key, "world")
            test_case.assertEqual(getattr(_service.llm_client, "profile_id", ""), "genesis-alt")
            test_case.assertEqual(getattr(_service.llm_client, "profile_name", ""), "Genesis Alt")
            test_case.assertEqual(getattr(_service.llm_client, "model", ""), "alt-model")
            test_case.assertEqual(getattr(_service.llm_client, "base_url", ""), "http://alt.invalid")
            test_case.assertIn("Genesis 总设计师", system_prompt)
            test_case.assertIn("【阶段】", user_prompt)
            test_case.assertIn("【阶段硬约束】", user_prompt)
            test_case.assertIn("【参考骨架】", user_prompt)
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

    def test_generate_stage_prompt_includes_locked_prior_stage_context(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis 锁定上下文书",
                    "premise": "地图生成时要显式参考已锁定的前序阶段。",
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
                            "overview": "旧城、荒原与遗迹并存。",
                            "axioms": ["力量必须付出代价"],
                            "history_slice": "旧王朝崩塌后的乱局时代。",
                            "naming_style": "中文短促命名。",
                            "forbidden_zones": ["避免现代网络梗"],
                        }
                    }
                }
            ),
        )
        api_module.lock_project_genesis_stage(created.project_id, "brief")
        api_module.lock_project_genesis_stage(created.project_id, "world")

        test_case = self

        def fake_generate_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            user_prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            test_case.assertEqual(stage_key, "map")
            test_case.assertIn("【已锁定阶段上下文（视为当前真值）】", user_prompt)
            test_case.assertIn('"stage_key": "brief"', user_prompt)
            test_case.assertIn('"stage_key": "world"', user_prompt)
            test_case.assertNotIn('"stage_key": "map"', user_prompt)
            test_case.assertIn("旧城、荒原与遗迹并存。", user_prompt)
            return (
                fallback,
                {
                    "effective_system_prompt": "genesis map",
                    "prompt_layers": [{"role": "system", "content": "genesis map"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fake-model"},
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_generate_call):
            detail = api_module.generate_project_genesis_stage(
                created.project_id,
                "map",
                BookGenesisStageRunRequest.model_validate({}),
            )

        self.assertIn("overview", detail.pack.world["map_atlas"])

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
                "brief",
                BookGenesisStageRunRequest.model_validate({}),
            )
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

    def test_fallback_genesis_preserves_named_entities_from_project_input(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "三十日回响压力测试",
                    "premise": (
                        "失业档案修复师林澈在第零日收到母亲十年前留下的空白遗书，随后发现整座城市"
                        "每过一天就会抹去一段公共记忆。为了在三十日内找回真相，他必须联合前调查记者许安、"
                        "企业继承人沈砚、地下算法师阿棠和失忆警员周岚，逐步揭开回声账本、临潮集团、"
                        "旧港火灾、母亲失踪和城市记忆循环之间的关系。"
                    ),
                    "setting_summary": (
                        "近未来海港城市「临潮」，旧城区、跨海企业、民间记忆馆、地下数据市场并存。"
                        "城市有一套名为回声账本的记忆审计系统，会把重大谎言记录为可追踪的回响。"
                    ),
                    "genre": "都市悬疑科幻",
                    "target_total_chapters": 30,
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
                "brief",
                BookGenesisStageRunRequest.model_validate({}),
            )
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

        world = detail.pack.world
        cast_names = {item["name"] for item in world["story_engine"]["core_cast"]}
        faction_names = {item["name"] for item in world["story_engine"]["factions"]}
        opposition_names = {item["name"] for item in world["story_engine"]["opposition"]}
        map_names = {
            *(item["name"] for item in world["map_atlas"]["submaps"]),
            *(item["name"] for item in world["map_atlas"]["regions"]),
            *(item["name"] for item in world["map_atlas"]["nodes"]),
        }

        self.assertIn("林澈", cast_names)
        self.assertTrue({"许安", "沈砚", "阿棠", "周岚"}.issubset(cast_names))
        self.assertIn("临潮集团", faction_names | opposition_names)
        self.assertIn("临潮", map_names)
        self.assertIn("旧城区", map_names)
        self.assertIn("民间记忆馆", map_names)
        self.assertNotIn("守仓阙微阑", cast_names)
        self.assertNotIn("礼川诸州", faction_names)

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
            system_prompt = "\n".join(item["content"] for item in messages if item["role"] == "system")
            user_prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            test_case.assertEqual(stage_key, "map:refine_item")
            test_case.assertIn("定向改写模式", system_prompt)
            test_case.assertIn("submaps[0]", user_prompt)
            test_case.assertIn("【目标路径】", user_prompt)
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
            system_prompt = "\n".join(item["content"] for item in messages if item["role"] == "system")
            user_prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            test_case.assertEqual(stage_key, "world:refine_item")
            test_case.assertIn("{\"value\": <更新后的 JSON 值>}", system_prompt)
            test_case.assertIn("history_slice", user_prompt)
            test_case.assertIn("【当前目标值】", user_prompt)
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

    def test_genesis_trace_records_selected_skills(self) -> None:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "Genesis Skill Trace 书",
                    "premise": "先生成世界观，再检查 Skill Trace。",
                    "genre": "玄幻",
                    "target_total_chapters": 8,
                }
            )
        )

        class FakeGenesisLLM:
            api_key = "test-key"
            profile_id = "default"
            profile_name = "Default"
            model = "fake-model"
            base_url = "http://example.invalid"

            def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
                return json.dumps(
                    {
                        "overview": "根世界观已建立。",
                        "axioms": ["力量必须付出代价。"],
                        "history_slice": "旧秩序正在松动。",
                        "naming_style": "中文网文风格。",
                        "forbidden_zones": [],
                        "culture_profiles": [
                            {
                                "id": "culture-main-stage",
                                "name": "主舞台文化",
                                "summary": "用于 Skill Trace 测试。",
                                "inspiration": "中华",
                                "generator_civilization": "中华",
                                "generator_overlays": [],
                                "character_name_examples": ["林烬"],
                                "region_name_examples": ["旧城核心区"],
                                "location_name_examples": ["旧城渡口"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )

        _registry, router, prompt_layer_builder = build_skill_runtime_components(
            root=Path(self.skill_root if hasattr(self, "skill_root") else Path(__file__).resolve().parents[1] / "forwin_skills"),
            enabled=True,
            strictness="normal",
        )
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = session.get(Project, created.project_id)
            revision = session.get(BookGenesisRevision, created.active_genesis_revision_id)
            assert project is not None
            assert revision is not None
            service = BookGenesisService(
                llm_client=FakeGenesisLLM(),
                skill_router=router,
                skill_prompt_layer_builder=prompt_layer_builder,
            )
            revision, _trace = service.generate_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key="world",
            )
            session.commit()

        with self.session_factory() as session:
            project = session.get(Project, created.project_id)
            assert project is not None
            revision = session.get(BookGenesisRevision, project.active_genesis_revision_id)
            assert revision is not None
            detail_pack = json.loads(revision.pack_json)
        self.assertEqual(detail_pack.get("world", {}).get("world_bible", {}).get("overview"), "根世界观已建立。")
        with self.session_factory() as session:
            trace = session.execute(
                select(PromptTrace)
                .where(PromptTrace.project_id == created.project_id, PromptTrace.stage_key == "world")
                .order_by(PromptTrace.created_at.desc(), PromptTrace.id.desc())
                .limit(1)
            ).scalar_one()

        prompt_layers = json.loads(trace.prompt_layers_json)
        input_snapshot = json.loads(trace.input_snapshot_json)
        output_summary = json.loads(trace.output_summary_json)
        self.assertTrue(input_snapshot.get("selected_skills"))
        self.assertTrue(output_summary.get("skill_summary"))
        self.assertTrue(any(item.get("kind") == "skill" for item in prompt_layers))


if __name__ == "__main__":
    unittest.main()
