from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

import forwin.api as api_module
from forwin import cli as cli_module
from forwin.config import Config
from forwin.director import ArcDirector
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.entity import EntityState
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.event import CanonEvent, EventEntityLink
from forwin.models.phase import (
    ArcEnvelope,
    ArcEnvelopeAnalysis,
    ArcStructureDraft,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalChapterLedger,
    ProvisionalBandExecution,
    ProvisionalPromotionRecord,
)
from forwin.models.phase4 import NPCIntentSnapshot, WorldSimulationTurn
from forwin.models.publisher import (
    PublisherBrowserSession,
    PublisherExtensionClient,
    PublisherRawComment,
    PublisherUploadJob,
)
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.orchestrator.phase3 import PacingAssessment, PacingStrategist, ReplanGovernor, StageAssessment
from forwin.orchestrator.phase24 import ArcEnvelopeManager
from forwin.orchestrator.phase4 import NPCIntentGenerator, WorldSimulator
from forwin.orchestrator.thread_sampling import sample_active_threads
from forwin.protocol.context import (
    ChapterContextPack,
    EntitySnapshot,
    MemorySnippet,
    NPCIntentView,
    PlotThreadSnapshot,
    ReaderCommentView,
    ReaderFeedbackView,
    RelationSnapshot,
    TimelineSnapshot,
    WorldPressureView,
)
from forwin.protocol.scene import SceneOutput, ScenePlan
from forwin.protocol.state_change import EventCandidate, StateChangeCandidate, ThreadBeatCandidate
from forwin.protocol.writer import WriterOutput
from forwin.publishers import PublisherManager
from forwin.retrieval import RetrievalBroker
from forwin.retrieval.memory_index import LocalChapterMemoryIndex, RemoteTextEmbedder, create_memory_index
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore
from forwin.utils import LLMJSONParseError, parse_llm_json
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.prompts import (
    build_scene_generation_prompt,
    build_single_chapter_draft_prompt,
)


class Phase05RegressionTests(unittest.TestCase):
    def test_config_from_env_reads_shared_fields_once(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FORWIN_DB_PATH": "tmp/test.db",
                "TEMPERATURE": "0.42",
                "MAX_TOKENS": "4096",
            },
            clear=False,
        ):
            config = Config.from_env()

        self.assertEqual(config.db_path, "tmp/test.db")
        self.assertEqual(config.temperature, 0.42)
        self.assertEqual(config.max_tokens, 4096)

    def test_config_from_env_reads_target_chapter_chars(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TARGET_CHAPTER_CHARS": "2600",
                "MIN_CHAPTER_CHARS": "1500",
                "MAX_CHAPTER_CHARS": "3200",
            },
            clear=False,
        ):
            config = Config.from_env()

        self.assertEqual(config.target_chapter_chars, 2600)

    def test_config_reads_embedding_and_pacing_controls(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FORWIN_EMBEDDING_BACKEND": "remote",
                "FORWIN_EMBEDDING_MODEL": "embed-test",
                "FORWIN_EMBEDDING_DIMS": "256",
                "PACING_MIN_AVG_CHARS": "1700",
                "PACING_MAX_AVG_CHARS": "3300",
                "BLACKBOX_WRITER_ATTENTION_RETRIES": "4",
                "PHASE4_USE_LLM": "false",
            },
            clear=False,
        ):
            config = Config.from_env()

        self.assertEqual(config.embedding_backend, "remote")
        self.assertEqual(config.embedding_model, "embed-test")
        self.assertEqual(config.embedding_dims, 256)
        self.assertEqual(config.pacing_min_avg_chars, 1700)
        self.assertEqual(config.pacing_max_avg_chars, 3300)
        self.assertEqual(config.blackbox_writer_attention_retries, 4)
        self.assertFalse(config.phase4_use_llm)

    def test_config_defaults_to_scene_writer_mode(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = Config.from_env()

        self.assertEqual(config.writer_mode, "scene")

    def test_cli_read_initializes_empty_database(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "fresh-read.db")
            output = StringIO()
            args = SimpleNamespace(
                db=db_path,
                api_key=None,
                model=None,
                base_url=None,
                project_id="missing-project",
                chapter=1,
            )

            with redirect_stdout(output):
                cli_module.cmd_read(args)

            self.assertIn("未找到项目 missing-project 的第 1 章计划。", output.getvalue())

    def test_cli_status_initializes_empty_database(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "fresh-status.db")
            output = StringIO()
            args = SimpleNamespace(
                db=db_path,
                api_key=None,
                model=None,
                base_url=None,
                project_id="missing-project",
            )

            with redirect_stdout(output):
                cli_module.cmd_status(args)

            self.assertIn("未找到项目: missing-project", output.getvalue())

    def test_orchestrator_surfaces_partial_failures(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "orchestrator.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="copilot",
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "测试失败路径",
                        "setting_summary": "无",
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "title": "第一章",
                                "one_line": "一",
                                "goals": ["g1"],
                            },
                            {
                                "chapter_number": 2,
                                "title": "第二章",
                                "one_line": "二",
                                "goals": ["g2"],
                            },
                        ],
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    if context.chapter_number == 2:
                        raise RuntimeError("simulated writer failure")
                    body = "正文" * 800
                    return WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter

                result = orchestrator.run("p", "g", 2)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    statuses = [
                        (plan.chapter_number, plan.status)
                        for plan in session.execute(
                            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
                        ).scalars()
                    ]
                    draft_count = session.execute(
                        select(func.count(ChapterDraft.id))
                    ).scalar_one()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "partial_failed")
            self.assertEqual(result.completed_chapters, [1])
            self.assertEqual(result.failed_chapters, [2])
            self.assertEqual(statuses, [(1, "accepted"), (2, "failed")])
            self.assertEqual(draft_count, 1)

    def test_writer_uses_target_chapter_chars_for_single_mode_prompt(self) -> None:
        class FakeLLMClient:
            def __init__(self):
                self.calls = []

            def chat(self, messages, *, temperature, max_tokens):
                self.calls.append(
                    {
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                )
                if len(self.calls) == 1:
                    return '{"title":"第一章","body":"' + ("正文" * 1200) + '","end_of_chapter_summary":"总结"}'
                return '{"state_changes":[],"new_events":[],"thread_beats":[]}'

        context = ChapterContextPack(
            project_id="proj-1",
            project_title="测试书",
            premise="前提",
            genre="玄幻",
            setting_summary="背景",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="开场",
            chapter_goals=["推进主线"],
        )
        client = FakeLLMClient()
        writer = ChapterWriter(
            client,
            writer_mode="single",
            min_chapter_chars=1500,
            max_chapter_chars=3200,
            target_chapter_chars=2600,
            max_tokens=5000,
        )

        writer.write_chapter(context)

        first_prompt = client.calls[0]["messages"][1]["content"]
        self.assertIn("目标正文长度 2600 到 3200 中文字", first_prompt)
        self.assertGreaterEqual(client.calls[0]["max_tokens"], 4600)

    def test_orchestrator_keeps_draft_when_structured_state_is_dirty(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "dirty-state.db")
            orchestrator = WritingOrchestrator(
                Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "测试脏状态回退",
                        "setting_summary": "无",
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "title": "第一章",
                                "one_line": "开场",
                                "goals": ["推进主线"],
                            }
                        ],
                        "characters": [
                            {
                                "name": "林夜",
                                "description": "主角",
                                "importance": 10,
                                "initial_state": {"location": "荒原", "status": "normal"},
                            }
                        ],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = "正文" * 800
                    return WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[
                            StateChangeCandidate(
                                entity_name="林夜",
                                entity_kind="character",
                                field="situation",
                                old_value="",
                                new_value="被盯上",
                                reason="测试脏字段",
                            )
                        ],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter

                result = orchestrator.run("p", "g", 1)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    statuses = [
                        (plan.chapter_number, plan.status)
                        for plan in session.execute(
                            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
                        ).scalars()
                    ]
                    draft_count = session.execute(
                        select(func.count(ChapterDraft.id))
                    ).scalar_one()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.completed_chapters, [1])
            self.assertEqual(result.failed_chapters, [])
            self.assertEqual(statuses, [(1, "accepted")])
            self.assertEqual(draft_count, 1)

    def test_orchestrator_checkpoint_mode_pauses_after_first_draft(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "checkpoint.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="checkpoint",
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "检查点模式",
                        "setting_summary": "无",
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "title": "第一章",
                                "one_line": "开场",
                                "goals": ["推进主线"],
                            },
                            {
                                "chapter_number": 2,
                                "title": "第二章",
                                "one_line": "继续",
                                "goals": ["继续推进"],
                            },
                        ],
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = "正文" * 800
                    return WriterOutput(
                        chapter_number=context.chapter_number,
                        title=f"第{context.chapter_number}章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter

                result = orchestrator.run("p", "g", 2)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(result.paused_chapters, [1])
            self.assertEqual(result.completed_chapters, [])

    def test_orchestrator_freezes_candidate_when_canon_update_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "freeze.db")
            artifact_root = str(Path(tmp) / "artifacts")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    artifact_root=artifact_root,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    freeze_failed_candidates=True,
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "冻结候选",
                        "setting_summary": "无",
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "title": "第一章",
                                "one_line": "开场",
                                "goals": ["推进主线"],
                            }
                        ],
                        "characters": [
                            {
                                "name": "林夜",
                                "description": "主角",
                                "importance": 10,
                                "initial_state": {"location": "荒原", "status": "normal"},
                            }
                        ],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = "正文" * 800
                    return WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[],
                        new_events=[
                            EventCandidate(
                                summary="未知角色闯入",
                                significance="major",
                                involved_entity_names=["不存在的人"],
                                roles=["protagonist"],
                            )
                        ],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter
                with patch.object(
                    StateUpdater,
                    "apply_thread_beats",
                    side_effect=ValueError("boom"),
                ):
                    result = orchestrator.run("p", "g", 1)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(len(result.frozen_artifacts), 1)
            self.assertEqual(result.paused_chapters, [1])
            self.assertTrue(Path(result.frozen_artifacts[0]).exists())

    def test_orchestrator_filters_unknown_event_entities_before_canon_update(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "filter-events.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    freeze_failed_candidates=True,
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "过滤未知事件实体",
                        "setting_summary": "无",
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "title": "第一章",
                                "one_line": "开场",
                                "goals": ["推进主线"],
                            }
                        ],
                        "characters": [
                            {
                                "name": "林夜",
                                "description": "主角",
                                "importance": 10,
                                "initial_state": {"location": "荒原", "status": "normal"},
                            }
                        ],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = "正文" * 800
                    return WriterOutput(
                        project_id="p1",
                        chapter_number=1,
                        title="第一章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[],
                        new_events=[
                            EventCandidate(
                                summary="未知角色闯入",
                                significance="major",
                                involved_entity_names=["不存在的人"],
                                roles=["protagonist"],
                            )
                        ],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter

                result = orchestrator.run("p", "g", 1)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    statuses = [
                        (plan.chapter_number, plan.status)
                        for plan in session.execute(
                            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
                        ).scalars()
                    ]
                    draft_count = session.execute(
                        select(func.count(ChapterDraft.id))
                    ).scalar_one()
                    event_count = session.execute(
                        select(func.count(CanonEvent.id))
                    ).scalar_one()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.frozen_artifacts, [])
            self.assertEqual(statuses, [(1, "accepted")])
            self.assertEqual(draft_count, 1)
            self.assertEqual(event_count, 0)

    def test_orchestrator_can_accept_review_and_continue_project(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "resume.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="checkpoint",
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "继续执行",
                        "setting_summary": "无",
                        "chapters": [
                            {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]},
                            {"chapter_number": 2, "title": "第二章", "one_line": "延续", "goals": ["继续推进"]},
                        ],
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = f"第{context.chapter_number}章正文" * 300
                    return WriterOutput(
                        chapter_number=context.chapter_number,
                        title=f"第{context.chapter_number}章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter

                initial = orchestrator.run("p", "g", 2)
                self.assertEqual(initial.status, "needs_review")

                approve = orchestrator.accept_review(initial.project_id, 1)
                self.assertIn("已接受并写入 canon", approve["message"])

                orchestrator.config = orchestrator.config.model_copy(update={"operation_mode": "blackbox"})
                resumed = orchestrator.continue_project(initial.project_id)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    statuses = [
                        (plan.chapter_number, plan.status)
                        for plan in session.execute(
                            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
                        ).scalars()
                    ]
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(resumed.status, "completed")
            self.assertEqual(resumed.completed_chapters, [2])
            self.assertEqual(statuses, [(1, "accepted"), (2, "accepted")])

    def test_orchestrator_records_phase3_analysis_and_replan_cooldown(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "phase3.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    pacing_window_size=3,
                    stale_thread_window=2,
                    replan_cooldown_chapters=3,
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "Phase3 测试",
                        "setting_summary": "无",
                        "chapters": [
                            {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]},
                            {"chapter_number": 2, "title": "第二章", "one_line": "继续", "goals": ["继续推进"]},
                            {"chapter_number": 3, "title": "第三章", "one_line": "承压", "goals": ["加压"]},
                            {"chapter_number": 4, "title": "第四章", "one_line": "转向", "goals": ["收束"]},
                        ],
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [
                            {
                                "name": "失踪信号",
                                "description": "主线线索",
                                "priority": 1,
                            }
                        ],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = f"第{context.chapter_number}章正文" * 280
                    return WriterOutput(
                        project_id=context.project_id,
                        chapter_number=context.chapter_number,
                        title=f"第{context.chapter_number}章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary=f"第{context.chapter_number}章摘要",
                        scene_outputs=[],
                        state_changes=[],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter

                result = orchestrator.run("p", "g", 4)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    analyses = session.execute(
                        select(ProjectStageAnalysis)
                        .order_by(ProjectStageAnalysis.chapter_number.asc())
                    ).scalars().all()
                    replan_events = session.execute(
                        select(ProjectReplanEvent)
                        .order_by(ProjectReplanEvent.trigger_chapter.asc())
                    ).scalars().all()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "completed")
            self.assertEqual([row.chapter_number for row in analyses], [1, 2, 3, 4])
            self.assertEqual(analyses[-1].stage_label, "finale")
            self.assertEqual([event.status for event in replan_events], ["applied", "cooldown"])
            self.assertEqual(replan_events[0].strategy, "patch")

    def test_replan_governor_supports_patch_reband_and_rearc(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "replan-strategy.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                arc = updater.create_arc_plan(project_id=project.id, arc_synopsis="初始大纲")
                for chapter_number in range(1, 6):
                    updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        one_line="原计划",
                        goals=["原目标"],
                    )
                session.commit()

                governor = ReplanGovernor(cooldown_chapters=1)

                patch_event = governor.apply_if_needed(
                    session=session,
                    project_id=project.id,
                    chapter_number=1,
                    stage=StageAssessment("opening", 0.2, "", 0),
                    pacing=PacingAssessment(
                        risk_level="high",
                        verdict="compressed",
                        summary="太短",
                        stale_threads=[],
                        active_thread_count=1,
                        unresolved_thread_count=0,
                        recent_char_counts=[1200, 1250, 1300],
                        recent_beat_count=1,
                    ),
                )
                session.commit()

                plan2 = session.execute(
                    select(ChapterPlan).where(
                        ChapterPlan.project_id == project.id,
                        ChapterPlan.chapter_number == 2,
                    )
                ).scalar_one()

                reband_event = governor.apply_if_needed(
                    session=session,
                    project_id=project.id,
                    chapter_number=3,
                    stage=StageAssessment("rising", 0.45, "", 0),
                    pacing=PacingAssessment(
                        risk_level="high",
                        verdict="stale_threads",
                        summary="一条线太久未动",
                        stale_threads=["失踪信号"],
                        active_thread_count=2,
                        unresolved_thread_count=1,
                        recent_char_counts=[1900, 2000, 2100],
                        recent_beat_count=1,
                    ),
                )
                session.commit()

                later_plans = session.execute(
                    select(ChapterPlan)
                    .where(
                        ChapterPlan.project_id == project.id,
                        ChapterPlan.chapter_number.in_((4, 5)),
                    )
                    .order_by(ChapterPlan.chapter_number.asc())
                ).scalars().all()

                rearc_event = governor.apply_if_needed(
                    session=session,
                    project_id=project.id,
                    chapter_number=5,
                    stage=StageAssessment("late", 0.9, "", 0),
                    pacing=PacingAssessment(
                        risk_level="high",
                        verdict="thread_drift",
                        summary="多条线同时漂移",
                        stale_threads=["主线", "支线"],
                        active_thread_count=3,
                        unresolved_thread_count=2,
                        recent_char_counts=[1900, 2000, 2100],
                        recent_beat_count=0,
                    ),
                )
                session.commit()

                active_arc = session.execute(
                    select(ArcPlanVersion)
                    .where(ArcPlanVersion.project_id == project.id, ArcPlanVersion.status == "active")
                    .order_by(ArcPlanVersion.version.desc())
                    .limit(1)
                ).scalar_one()
            finally:
                session.close()
                engine.dispose()

            self.assertIsNotNone(patch_event)
            self.assertEqual(patch_event.strategy, "patch")
            self.assertIn("patch", plan2.one_line)
            self.assertIn("优先修补节奏风险", plan2.goals_json)
            self.assertIsNotNone(reband_event)
            self.assertEqual(reband_event.strategy, "reband")
            self.assertTrue(all("reband" in plan.one_line for plan in later_plans))
            self.assertIsNotNone(rearc_event)
            self.assertEqual(rearc_event.strategy, "rearc")
            self.assertIn("Phase3 replan", active_arc.arc_synopsis)

    def test_pacing_strategist_respects_configured_thresholds(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "pacing-thresholds.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                arc = updater.create_arc_plan(project_id=project.id, arc_synopsis="测试")
                plans = []
                for chapter_number in range(1, 4):
                    plans.append(
                        updater.create_chapter_plan(
                            project_id=project.id,
                            arc_plan_id=arc.id,
                            chapter_number=chapter_number,
                            title=f"第{chapter_number}章",
                            one_line="测试",
                            goals=["推进"],
                        )
                    )
                session.flush()
                for plan in plans:
                    session.add(
                        ChapterDraft(
                            chapter_plan_id=plan.id,
                            version=1,
                            body_text="正文" * 900,
                            char_count=1800,
                            summary="摘要",
                            llm_raw_response="{}",
                            llm_model="fake",
                        )
                    )
                session.commit()

                strategist = PacingStrategist(
                    window_size=3,
                    stale_thread_window=3,
                    min_avg_chars=2000,
                    max_avg_chars=2400,
                )
                assessment = strategist.analyze(
                    session=session,
                    project_id=project.id,
                    chapter_number=3,
                )
            finally:
                session.close()
                engine.dispose()

            self.assertEqual(assessment.verdict, "compressed")
            self.assertEqual(assessment.risk_level, "high")

    def test_api_projects_include_phase3_summary_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "phase3-api.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                session.add(
                    ProjectStageAnalysis(
                        project_id=project.id,
                        chapter_number=2,
                        stage_label="midpoint",
                        progress_ratio=0.5,
                        timeline_label="第二日夜",
                        timeline_ordinal=2,
                        pacing_verdict="steady",
                        pacing_summary="最近章节推进均衡",
                        stale_threads_json="[]",
                        active_thread_count=1,
                        unresolved_thread_count=0,
                    )
                )
                session.add(
                    ProjectReplanEvent(
                        project_id=project.id,
                        trigger_chapter=2,
                        risk_level="high",
                        reason="测试 replan",
                        focus_threads_json='["主线"]',
                        status="applied",
                        cooldown_until_chapter=5,
                    )
                )
                session.commit()

                old_engine = api_module._engine
                old_factory = api_module._SessionFactory
                try:
                    api_module._engine = engine
                    api_module._SessionFactory = get_session_factory(engine)
                    payload = api_module.list_projects()
                    detail = api_module.get_project(project.id)
                finally:
                    api_module._engine = old_engine
                    api_module._SessionFactory = old_factory
            finally:
                session.close()
                engine.dispose()

            self.assertEqual(payload[0].latest_stage, "midpoint")
            self.assertEqual(payload[0].last_replan_status, "applied")
            self.assertEqual(detail.current_time_label, "第二日夜")
            self.assertEqual(detail.recent_replans[0]["status"], "applied")
            self.assertEqual(payload[0].chapters, [])

    def test_api_projects_include_chapter_summaries(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "projects-with-chapters.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                arc = updater.create_arc_plan(project.id, "剧情")
                first_plan = updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    one_line="开场",
                    goals=["推进"],
                )
                second_plan = updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=2,
                    title="第二章",
                    one_line="转折",
                    goals=["爆发"],
                )
                session.add(
                    ChapterDraft(
                        chapter_plan_id=first_plan.id,
                        version=1,
                        body_text="正文一",
                        summary="摘要一",
                        char_count=100,
                    )
                )
                session.add(
                    ChapterDraft(
                        chapter_plan_id=second_plan.id,
                        version=1,
                        body_text="正文二",
                        summary="摘要二",
                        char_count=200,
                    )
                )
                session.commit()

                old_engine = api_module._engine
                old_factory = api_module._SessionFactory
                try:
                    api_module._engine = engine
                    api_module._SessionFactory = get_session_factory(engine)
                    payload = api_module.list_projects()
                finally:
                    api_module._engine = old_engine
                    api_module._SessionFactory = old_factory
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(len(payload), 1)
        self.assertEqual(
            [row["chapter_number"] for row in payload[0].chapters],
            [1, 2],
        )
        self.assertEqual(payload[0].chapters[0]["summary"], "摘要一")
        self.assertEqual(payload[0].chapters[1]["char_count"], 200)

    def test_local_memory_index_persists_and_searches_across_instances(self) -> None:
        with TemporaryDirectory() as tmp:
            index = LocalChapterMemoryIndex(root_dir=tmp)
            index.upsert_chapter(
                project_id="p1",
                chapter_number=1,
                title="幽灵列车",
                summary="失踪三年的末班车忽然回归",
                body="沈砚在雨夜看到那辆本不该出现的末班车。",
            )
            index.upsert_chapter(
                project_id="p1",
                chapter_number=2,
                title="废弃仓库",
                summary="众人转去调查旧仓库线索",
                body="他们在仓库里翻找旧档案。",
            )

            reloaded = LocalChapterMemoryIndex(root_dir=tmp)
            hits = reloaded.search(project_id="p1", query="末班车", limit=2)

        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].chapter_number, 1)
        self.assertIn("末班车", hits[0].summary)

    def test_thread_sampling_balances_stale_and_hot_threads(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "thread-sampling.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                stale_primary = updater.create_thread(project.id, "主线悬案", "主线", priority=1, chapter=1)
                hot_followup = updater.create_thread(project.id, "新线索", "跟进", priority=4, chapter=4)
                stale_secondary = updater.create_thread(project.id, "旧恩怨", "旧冲突", priority=2, chapter=1)
                updater.create_thread(project.id, "背景调查", "背景", priority=5, chapter=3)

                updater.apply_thread_beats(
                    project.id,
                    1,
                    [ThreadBeatCandidate(thread_name=stale_primary.name, beat_type="setup", description="铺设主线")],
                )
                updater.apply_thread_beats(
                    project.id,
                    2,
                    [ThreadBeatCandidate(thread_name=stale_secondary.name, beat_type="setup", description="铺设旧恩怨")],
                )
                updater.apply_thread_beats(
                    project.id,
                    5,
                    [ThreadBeatCandidate(thread_name=hot_followup.name, beat_type="escalation", description="新线索升温")],
                )
                session.commit()

                sampled = sample_active_threads(
                    session=session,
                    project_id=project.id,
                    chapter_number=5,
                    limit=3,
                    stale_window=2,
                    recent_window=2,
                )
            finally:
                session.close()
                engine.dispose()

        sampled_names = [thread.name for thread in sampled.threads]
        self.assertIn("主线悬案", sampled_names)
        self.assertIn("新线索", sampled_names)
        self.assertLessEqual(len(sampled_names), 3)

    def test_arc_envelope_manager_creates_v24_records(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "arc-envelope.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                arc = updater.create_arc_plan(project.id, "主线弧")
                for chapter_number in range(1, 11):
                    updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        one_line=f"推进第{chapter_number}章冲突",
                        goals=["推进", "升级"],
                    )
                manager = ArcEnvelopeManager(
                    director=None,
                    provisional_executor=lambda **kwargs: {
                        "band_id": kwargs["band_id"],
                        "artifact_path": "provisional://preview.json",
                        "aggregate_verdict": "warn",
                        "preview_chapter_count": 2,
                        "total_char_count": 3200,
                        "issue_count": 1,
                        "failure_count": 0,
                        "chapter_numbers": [1, 2],
                        "summary_lines": ["预演一", "预演二"],
                    },
                )
                envelope = manager.ensure_active_arc_resolution(
                    session=session,
                    project_id=project.id,
                    activation_chapter=1,
                )
                session.flush()

                structure_count = session.execute(
                    select(func.count(ArcStructureDraft.id))
                ).scalar_one()
                analysis_count = session.execute(
                    select(func.count(ArcEnvelopeAnalysis.id))
                ).scalar_one()
                provisional_count = session.execute(
                    select(func.count(ProvisionalBandExecution.id))
                ).scalar_one()
            finally:
                session.close()
                engine.dispose()

        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.source_policy_tier, "short")
        self.assertGreaterEqual(envelope.resolved_target_size, 10)
        self.assertGreaterEqual(envelope.detailed_band_size, 4)
        self.assertEqual(structure_count, 1)
        self.assertEqual(analysis_count, 1)
        self.assertEqual(provisional_count, 1)

    def test_api_projects_expose_active_arc_envelope_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "arc-envelope-api.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                arc = updater.create_arc_plan(project.id, "主线弧")
                session.add(
                    ArcEnvelope(
                        project_id=project.id,
                        arc_id=arc.id,
                        base_target_size=18,
                        base_soft_min=14,
                        base_soft_max=23,
                        resolved_target_size=20,
                        resolved_soft_min=15,
                        resolved_soft_max=24,
                        detailed_band_size=8,
                        frozen_zone_size=3,
                        current_projected_size=20,
                        current_confidence=0.82,
                        source_policy_tier="short",
                    )
                )
                session.commit()

                old_engine = api_module._engine
                old_factory = api_module._SessionFactory
                try:
                    api_module._engine = engine
                    api_module._SessionFactory = get_session_factory(engine)
                    payload = api_module.list_projects()
                    detail = api_module.get_project(project.id)
                finally:
                    api_module._engine = old_engine
                    api_module._SessionFactory = old_factory
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(payload[0].active_arc_policy_tier, "short")
        self.assertEqual(payload[0].active_arc_target_size, 20)
        self.assertEqual(detail.active_arc_detailed_band_size, 8)
        self.assertEqual(detail.active_arc_frozen_zone_size, 3)

    def test_api_exposes_latest_provisional_band_shadow_ledger(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "provisional-api.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                arc = updater.create_arc_plan(project.id, "主线弧")
                session.add(
                    ProvisionalBandExecution(
                        project_id=project.id,
                        arc_id=arc.id,
                        band_id="band:1:6",
                        chapter_numbers_json="[1, 2]",
                        artifact_path="provisional://band.json",
                        aggregate_verdict="warn",
                        preview_char_count=3200,
                        issue_count=2,
                        failure_count=0,
                    )
                )
                session.add(
                    ProvisionalChapterLedger(
                        project_id=project.id,
                        arc_id=arc.id,
                        band_id="band:1:6",
                        chapter_number=1,
                        title="第一章",
                        summary="影子摘要",
                        verdict="warn",
                        char_count=1600,
                        artifact_meta_path="meta://1",
                        draft_blob_path="draft://1",
                        current_time_label="第一日夜",
                        projected_time_label="第二日晨",
                        state_changes_json='[{"entity_name":"林夜"}]',
                        events_json='[{"summary":"发现异动"}]',
                        thread_beats_json='[{"thread_name":"主线"}]',
                        time_advance_json='{"new_time_label":"第二日晨"}',
                        issues_json='[{"rule_name":"len","severity":"warning","description":"略短"}]',
                    )
                )
                session.commit()

                old_engine = api_module._engine
                old_factory = api_module._SessionFactory
                try:
                    api_module._engine = engine
                    api_module._SessionFactory = get_session_factory(engine)
                    payload = api_module.get_latest_provisional_band(project.id)
                finally:
                    api_module._engine = old_engine
                    api_module._SessionFactory = old_factory
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(payload.band_id, "band:1:6")
        self.assertEqual(payload.aggregate_verdict, "warn")
        self.assertEqual(payload.chapters[0].projected_time_label, "第二日晨")
        self.assertEqual(payload.chapters[0].events[0]["summary"], "发现异动")
        self.assertEqual(payload.chapters[0].issues[0]["rule_name"], "len")

    def test_api_list_chapters_uses_latest_draft_values(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "chapter-list.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                arc = updater.create_arc_plan(project.id, "剧情")
                plan = updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    one_line="开场",
                    goals=["推进"],
                )
                session.add(
                    ChapterDraft(
                        chapter_plan_id=plan.id,
                        version=1,
                        body_text="旧正文",
                        summary="旧摘要",
                        char_count=10,
                    )
                )
                session.add(
                    ChapterDraft(
                        chapter_plan_id=plan.id,
                        version=2,
                        body_text="新正文",
                        summary="新摘要",
                        char_count=20,
                    )
                )
                session.commit()

                old_engine = api_module._engine
                old_factory = api_module._SessionFactory
                try:
                    api_module._engine = engine
                    api_module._SessionFactory = get_session_factory(engine)
                    payload = api_module.list_chapters(project.id)
                finally:
                    api_module._engine = old_engine
                    api_module._SessionFactory = old_factory
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0].char_count, 20)
        self.assertEqual(payload[0].summary, "新摘要")

    def test_phase4_prompt_includes_npc_intents_and_world_pressure(self) -> None:
        context = ChapterContextPack(
            project_id="p1",
            project_title="书",
            genre="玄幻",
            premise="前提",
            setting_summary="设定",
            chapter_number=3,
            chapter_plan_title="第三章",
            chapter_plan_one_line="危机加深",
            chapter_goals=["推进主线"],
            npc_intents=[
                NPCIntentView(
                    entity_name="林夜",
                    intent_kind="pursue",
                    objective="围绕失踪信号采取行动",
                    tactic="制造信息差",
                    urgency=5,
                    notes="下章前生效",
                )
            ],
            world_pressure=WorldPressureView(
                pressure_level="rising",
                pressure_summary="悬置线程开始反噬主角。",
                notable_shifts=["失踪信号开始扩散"],
            ),
        )
        prompt = build_single_chapter_draft_prompt(
            context,
            target_chars=2000,
            min_chars=1500,
            max_chars=2200,
        )
        self.assertIn("【NPC 当前意图】", prompt[1]["content"])
        self.assertIn("【世界压力】", prompt[1]["content"])

        scene_prompt = build_scene_generation_prompt(
            context,
            ScenePlan(
                scene_no=1,
                objective="追查",
                must_progress_points=["发现异常"],
                time_hint="深夜",
                location_hint="站台",
                involved_entities=["林夜"],
                micro_hook="看到第三个人",
                target_chars=800,
            ),
        )
        self.assertIn("当前 NPC 意图", scene_prompt[1]["content"])
        self.assertIn("世界压力", scene_prompt[1]["content"])

    def test_state_repository_active_entities_uses_latest_state_and_alias_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "repo-entities.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                entity = updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="沈砚",
                    description="主角",
                    aliases=["阿砚", "记者"],
                    importance=9,
                    chapter=1,
                )
                updater.create_entity_state(
                    entity_id=entity.id,
                    chapter=1,
                    state={"location": "旧站台", "status": "normal"},
                )
                updater.create_entity_state(
                    entity_id=entity.id,
                    chapter=2,
                    state={"location": "新站台", "status": "alert"},
                )
                session.commit()

                repo = StateRepository(session)
                snapshots = repo.get_active_entities(project.id)
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(sorted(snapshots[0].aliases), ["记者", "阿砚"])
        self.assertEqual(snapshots[0].current_state["location"], "新站台")
        self.assertEqual(snapshots[0].current_state["status"], "alert")

    def test_prompt_includes_reader_feedback(self) -> None:
        context = ChapterContextPack(
            project_id="p1",
            project_title="书",
            genre="玄幻",
            premise="前提",
            setting_summary="设定",
            chapter_number=2,
            chapter_plan_title="第二章",
            chapter_plan_one_line="继续推进",
            chapter_goals=["推进主线"],
            reader_feedback=ReaderFeedbackView(
                comment_count=3,
                dominant_sentiment="curious",
                feedback_summary="最近 3 条评论，读者对悬念追问较多。",
                recent_highlights=[
                    ReaderCommentView(platform_id="qidian", author_name="读者A", body_text="为什么林夜还不反击？"),
                ],
                highlighted_topics=["悬念追问"],
            ),
        )
        prompt = build_single_chapter_draft_prompt(
            context,
            target_chars=1800,
            min_chars=1500,
            max_chars=2200,
        )
        self.assertIn("【读者反馈】", prompt[1]["content"])
        self.assertIn("为什么林夜还不反击", prompt[1]["content"])

    def test_phase4_generators_can_use_llm_output(self) -> None:
        class FakeIntentLLM:
            def chat(self, messages, temperature: float, max_tokens: int, response_format=None):
                return '{"intents":[{"entity_name":"林夜","intent_kind":"pressure","objective":"逼近失踪信号核心","tactic":"先制造信息差再逼问","urgency":5,"notes":"下章立即执行"}]}'

        class FakeWorldLLM:
            def chat(self, messages, temperature: float, max_tokens: int, response_format=None):
                return '{"pressure_level":"critical","pressure_summary":"世界开始围堵主角。","notable_shifts":["失踪信号扩散","同盟开始动摇"]}'

        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "phase4-llm.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="林夜",
                    description="主角",
                    importance=10,
                )
                updater.create_thread(
                    project_id=project.id,
                    name="失踪信号",
                    description="主线",
                    priority=1,
                    chapter=0,
                )
                session.commit()

                intents = NPCIntentGenerator(llm_client=FakeIntentLLM()).generate(
                    session=session,
                    project_id=project.id,
                    chapter_number=1,
                )
                world = WorldSimulator(llm_client=FakeWorldLLM()).simulate(
                    session=session,
                    project_id=project.id,
                    chapter_number=1,
                )
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(intents[0].intent_kind, "pressure")
        self.assertIn("逼近失踪信号核心", intents[0].objective)
        self.assertEqual(world.pressure_level, "critical")
        self.assertIn("围堵主角", world.pressure_summary)

    def test_orchestrator_records_phase4_outputs(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "phase4.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "Phase4 测试",
                        "setting_summary": "无",
                        "chapters": [
                            {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]},
                            {"chapter_number": 2, "title": "第二章", "one_line": "继续", "goals": ["继续推进"]},
                        ],
                        "characters": [
                            {
                                "name": "林夜",
                                "description": "主角",
                                "importance": 10,
                                "initial_state": {"location": "旧站台", "status": "normal"},
                            },
                            {
                                "name": "周柒",
                                "description": "搭档",
                                "importance": 7,
                                "initial_state": {"location": "调度室", "status": "normal"},
                            },
                        ],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [
                            {"name": "失踪信号", "description": "主线线索", "priority": 1}
                        ],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write(context) -> WriterOutput:
                    body = f"第{context.chapter_number}章正文" * 320
                    return WriterOutput(
                        project_id=context.project_id,
                        chapter_number=context.chapter_number,
                        title=f"第{context.chapter_number}章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary=f"第{context.chapter_number}章摘要",
                        state_changes=[],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write
                result = orchestrator.run("p", "g", 2)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    intents = session.execute(
                        select(NPCIntentSnapshot).order_by(NPCIntentSnapshot.chapter_number.asc())
                    ).scalars().all()
                    turns = session.execute(
                        select(WorldSimulationTurn).order_by(WorldSimulationTurn.chapter_number.asc())
                    ).scalars().all()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "completed")
            self.assertGreaterEqual(len(intents), 2)
            self.assertEqual([turn.chapter_number for turn in turns], [1, 2])

    def test_repo_summarizes_reader_feedback_by_work_name(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "reader-feedback.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                session.add(
                    PublisherRawComment(
                        platform_id="qidian",
                        remote_comment_id="c1",
                        work_name="测试书",
                        chapter_title="第一章",
                        author_name="读者A",
                        body_text="这章有点拖，不过悬念还是很强，为什么还不揭晓？",
                    )
                )
                session.add(
                    PublisherRawComment(
                        platform_id="fanqie",
                        remote_comment_id="c2",
                        work_name="测试书",
                        chapter_title="第一章",
                        author_name="读者B",
                        body_text="很好看，期待林夜下一章反击！",
                    )
                )
                session.commit()

                from forwin.state.repo import StateRepository
                feedback = StateRepository(session).get_recent_reader_feedback(
                    project.id,
                    before_chapter=2,
                )
            finally:
                session.close()
                engine.dispose()

        self.assertIsNotNone(feedback)
        assert feedback is not None
        self.assertEqual(feedback.comment_count, 2)
        self.assertTrue(feedback.feedback_summary)
        self.assertGreaterEqual(len(feedback.recent_highlights), 1)

    def test_phase4_rule_fallback_uses_reader_feedback(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "phase4-feedback.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="林夜",
                    description="主角",
                    importance=10,
                )
                updater.create_thread(
                    project_id=project.id,
                    name="失踪信号",
                    description="主线",
                    priority=1,
                    chapter=0,
                )
                session.add(
                    PublisherRawComment(
                        platform_id="qidian",
                        remote_comment_id="c1",
                        work_name="测试书",
                        chapter_title="第一章",
                        author_name="读者A",
                        body_text="最近有点拖，为什么还不推进主线？",
                    )
                )
                session.commit()

                intents = NPCIntentGenerator(llm_client=None).generate(
                    session=session,
                    project_id=project.id,
                    chapter_number=2,
                )
                world = WorldSimulator(llm_client=None).simulate(
                    session=session,
                    project_id=project.id,
                    chapter_number=2,
                )
            finally:
                session.close()
                engine.dispose()

        self.assertTrue(any("节奏" in item.tactic or "悬念" in item.tactic for item in intents))
        self.assertIn(world.pressure_level, {"rising", "critical"})

    def test_blackbox_writer_failure_degrades_to_needs_review(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "blackbox-attention.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    blackbox_writer_attention_retries=2,
                    freeze_failed_candidates=True,
                )
            )
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "黑箱失败",
                    "setting_summary": "无",
                    "chapters": [
                        {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}
                    ],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }

                def always_fail(_context):
                    raise RuntimeError("writer exploded")

                orchestrator.writer.write_chapter = always_fail
                result = orchestrator.run("p", "g", 1)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    plan = session.execute(select(ChapterPlan)).scalar_one()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(result.paused_chapters, [1])
            self.assertEqual(plan.status, "needs_review")
            self.assertEqual(len(result.frozen_artifacts), 1)

    def test_blackbox_timeout_stops_extra_writer_retries(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "blackbox-timeout.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    blackbox_writer_attention_retries=3,
                    freeze_failed_candidates=True,
                )
            )
            calls = {"count": 0}
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "黑箱超时",
                    "setting_summary": "无",
                    "chapters": [
                        {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}
                    ],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }

                def always_timeout(_context):
                    calls["count"] += 1
                    raise RuntimeError("The read operation timed out")

                orchestrator.writer.write_chapter = always_timeout
                result = orchestrator.run("p", "g", 1)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(calls["count"], 1)

    def test_api_project_detail_includes_phase4_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "phase4-api.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                entity = updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="林夜",
                    description="主角",
                )
                session.add(
                    WorldSimulationTurn(
                        project_id=project.id,
                        chapter_number=2,
                        pressure_level="rising",
                        pressure_summary="悬置线程开始反噬主角。",
                        notable_shifts_json='["失踪信号开始扩散"]',
                    )
                )
                session.add(
                    NPCIntentSnapshot(
                        project_id=project.id,
                        chapter_number=2,
                        entity_id=entity.id,
                        entity_name="林夜",
                        intent_kind="pursue",
                        objective="围绕失踪信号采取行动",
                        tactic="制造信息差",
                        urgency=5,
                        notes="下章前生效",
                    )
                )
                session.commit()

                old_engine = api_module._engine
                old_factory = api_module._SessionFactory
                try:
                    api_module._engine = engine
                    api_module._SessionFactory = get_session_factory(engine)
                    detail = api_module.get_project(project.id)
                finally:
                    api_module._engine = old_engine
                    api_module._SessionFactory = old_factory
            finally:
                session.close()
                engine.dispose()

            self.assertEqual(detail.world_pressure_level, "rising")
            self.assertEqual(detail.npc_intent_count, 1)
            self.assertEqual(detail.recent_npc_intents[0]["entity_name"], "林夜")

    def test_api_project_summary_includes_replan_strategy(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "phase3-strategy-api.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                session.add(
                    ProjectReplanEvent(
                        project_id=project.id,
                        trigger_chapter=2,
                        risk_level="high",
                        reason="测试 replan",
                        focus_threads_json='["主线"]',
                        strategy="reband",
                        status="applied",
                        cooldown_until_chapter=5,
                    )
                )
                session.commit()

                old_engine = api_module._engine
                old_factory = api_module._SessionFactory
                try:
                    api_module._engine = engine
                    api_module._SessionFactory = get_session_factory(engine)
                    payload = api_module.list_projects()
                    detail = api_module.get_project(project.id)
                finally:
                    api_module._engine = old_engine
                    api_module._SessionFactory = old_factory
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(payload[0].last_replan_strategy, "reband")
        self.assertEqual(detail.recent_replans[0]["strategy"], "reband")

    def test_apply_events_rejects_partial_event_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "events.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")

                with self.assertRaisesRegex(ValueError, "unknown entity"):
                    updater.apply_events(
                        project_id=project.id,
                        chapter_number=1,
                        events=[
                            EventCandidate(
                                summary="未知角色出现",
                                significance="major",
                                involved_entity_names=["不存在的人"],
                                roles=["protagonist"],
                            )
                        ],
                    )

                session.rollback()

                event_count = session.execute(
                    select(func.count(CanonEvent.id))
                ).scalar_one()
                link_count = session.execute(
                    select(func.count(EventEntityLink.id))
                ).scalar_one()
            finally:
                session.close()
                engine.dispose()

            self.assertEqual(event_count, 0)
            self.assertEqual(link_count, 0)

    def test_arc_director_is_independent_from_writer(self) -> None:
        class FakeLLMClient:
            def __init__(self) -> None:
                self.last_messages = None

            def chat(self, messages, temperature: float, max_tokens: int) -> str:
                self.last_messages = messages
                return (
                    '{"arc_synopsis":"弧线","setting_summary":"设定","chapters":[{"chapter_number":1,'
                    '"title":"第一章","one_line":"开场","goals":["目标"]}],"characters":[],'
                    '"locations":[],"factions":[],"relations":[],"plot_threads":[],"initial_time":'
                    '{"label":"开始","description":"开场"}}'
                )

        client = FakeLLMClient()
        director = ArcDirector(client, max_tokens=2048)

        plan = director.plan_arc("故事前提", "玄幻", 1)

        self.assertEqual(plan["arc_synopsis"], "弧线")
        self.assertEqual(plan["chapters"][0]["title"], "第一章")
        self.assertEqual(len(client.last_messages), 2)

    def test_single_chapter_prompt_uses_configurable_char_bounds(self) -> None:
        context = ChapterContextPack(
            project_id="p1",
            project_title="书",
            genre="玄幻",
            premise="前提",
            setting_summary="设定",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="开场",
            chapter_goals=["推进主线"],
            previous_chapter_summaries=[],
            active_entities=[],
            active_relations=[],
            active_threads=[],
            timeline=None,
        )

        prompt = build_single_chapter_draft_prompt(
            context,
            target_chars=2600,
            min_chars=1800,
            max_chars=3200,
        )

        self.assertIn(
            "目标正文长度 2600 到 3200 中文字，不得低于 1800 中文字",
            prompt[1]["content"],
        )

    def test_parse_llm_json_marks_empty_response(self) -> None:
        with self.assertRaises(LLMJSONParseError) as raised:
            parse_llm_json("<think>internal</think>", error_prefix="test")

        self.assertTrue(raised.exception.empty_response)

    def test_parse_llm_json_repairs_fenced_truncated_object(self) -> None:
        raw = """```json
{
  "scenes": [
    {
      "scene_no": 1,
      "objective": "建立异象",
      "must_progress_points": ["主角抵达站台"]
    }
  ]
"""

        parsed = parse_llm_json(raw, error_prefix="test")

        self.assertEqual(parsed["scenes"][0]["scene_no"], 1)
        self.assertEqual(parsed["scenes"][0]["objective"], "建立异象")

    def test_retrieval_broker_keeps_high_priority_threads_when_trimming(self) -> None:
        class FakeRepo:
            pass

        chapter_plan = SimpleNamespace(
            chapter_number=1,
            title="第一章",
            one_line="开场",
            goals_json='["推进主线"]',
        )
        broker = RetrievalBroker(context_budget_chars=420, max_threads=4)
        broker._estimate_chars = lambda _pack: 999  # type: ignore[method-assign]

        with patch("forwin.retrieval.broker.assemble_context") as mocked:
            mocked.return_value = ChapterContextPack(
                project_id="p1",
                project_title="书",
                genre="玄幻",
                premise="前提",
                setting_summary="设定",
                chapter_number=1,
                chapter_plan_title="第一章",
                chapter_plan_one_line="开场",
                chapter_goals=["推进主线"],
                previous_chapter_summaries=["a", "b"],
                active_entities=[],
                active_relations=[],
                active_threads=[
                    PlotThreadSnapshot(thread_id="t1", name="高优先", description="A", status="active", priority=1),
                    PlotThreadSnapshot(thread_id="t2", name="低优先", description="B", status="active", priority=3),
                ],
                timeline=None,
            )
            pack = broker.build_chapter_context(FakeRepo(), "p1", chapter_plan)

        self.assertEqual([thread.name for thread in pack.active_threads], ["高优先"])

    def test_typed_state_schema_rejects_unknown_new_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                entity = updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="林夜",
                    description="主角",
                )
                updater.create_entity_state(
                    entity_id=entity.id,
                    chapter=0,
                    state={"location": "黑井聚落", "status": "normal"},
                )
                session.commit()

                with self.assertRaisesRegex(ValueError, "Unsupported state field"):
                    updater.apply_state_changes(
                        project_id=project.id,
                        chapter_number=1,
                        changes=[
                            StateChangeCandidate(
                                entity_name="林夜",
                                entity_kind="character",
                                field="mystery_rank",
                                old_value="",
                                new_value="S",
                                reason="测试",
                            )
                        ],
                    )

                session.rollback()

                state_rows = session.execute(
                    select(EntityState).where(EntityState.entity_id == entity.id)
                ).scalars().all()
            finally:
                session.close()
                engine.dispose()

            self.assertEqual(len(state_rows), 1)

    def test_repo_resolves_entity_alias_without_full_entity_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "aliases.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                entity = updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="林夜",
                    description="主角",
                    aliases=["夜哥", "林哥"],
                )
                session.commit()

                repo = updater._repo
                resolved = repo.get_entity_by_name(project.id, "夜哥")
            finally:
                session.close()
                engine.dispose()

            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.id, entity.id)

    def test_writer_marks_structured_extraction_as_degraded_after_retry_failure(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, temperature: float, max_tokens: int, response_format=None) -> str:
                self.calls += 1
                if self.calls == 1:
                    return '{"title":"第一章","body":"%s"}' % ("正文" * 900)
                raise RuntimeError("structured extraction failed")

        writer = ChapterWriter(FakeClient(), writer_mode="single")
        context = ChapterContextPack(
            project_id="project-structured",
            project_title="测试书",
            premise="前提",
            genre="玄幻",
            setting_summary="设定",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="开场",
            chapter_goals=["推进主线"],
        )

        output = writer.write_chapter(context)

        self.assertEqual(output.project_id, "project-structured")
        self.assertEqual(output.generation_meta["structured_extraction"], "degraded")
        self.assertEqual(output.new_events, [])
        self.assertEqual(output.state_changes, [])

    def test_retrieval_broker_applies_budget(self) -> None:
        broker = RetrievalBroker(
            context_budget_chars=1000,
            max_entities=2,
            max_threads=1,
            max_summaries=2,
        )

        class FakeRepo:
            def get_project(self, project_id):
                return SimpleNamespace(
                    title="书",
                    premise="设定" * 20,
                    genre="玄幻",
                    setting_summary="背景" * 20,
                )

            def get_active_entities(self, project_id):
                return [
                    EntitySnapshot(
                        entity_id="e1",
                        kind="character",
                        name="甲",
                        importance=9,
                        aliases=[],
                        description="主角" * 20,
                        current_state={"location": "A", "status": "normal"},
                    ),
                    EntitySnapshot(
                        entity_id="e2",
                        kind="character",
                        name="乙",
                        importance=8,
                        aliases=[],
                        description="配角" * 20,
                        current_state={"location": "B", "status": "normal"},
                    ),
                    EntitySnapshot(
                        entity_id="e3",
                        kind="character",
                        name="丙",
                        importance=1,
                        aliases=[],
                        description="路人" * 20,
                        current_state={"location": "C", "status": "normal"},
                    ),
                ]

            def get_active_relations(self, project_id):
                return [
                    RelationSnapshot(
                        source_name="甲",
                        target_name="乙",
                        relation_type="盟友",
                        description="一起行动",
                    ),
                    RelationSnapshot(
                        source_name="丙",
                        target_name="路人甲",
                        relation_type="路过",
                        description="无关",
                    ),
                ]

            def get_active_threads(self, project_id):
                return [
                    PlotThreadSnapshot(
                        thread_id="t1",
                        name="主线",
                        description="主线" * 20,
                        status="active",
                        priority=1,
                        recent_beats=["推进一", "推进二"],
                    ),
                    PlotThreadSnapshot(
                        thread_id="t2",
                        name="支线",
                        description="支线" * 20,
                        status="active",
                        priority=3,
                        recent_beats=["支线"],
                    ),
                ]

            def get_chapter_summaries(self, project_id, chapter_number):
                return ["摘要一" * 20, "摘要二" * 20, "摘要三" * 20]

            def get_current_timeline(self, project_id):
                return TimelineSnapshot(current_time_label="第一天", ordinal=1)

        chapter_plan = SimpleNamespace(
            chapter_number=3,
            title="第三章",
            one_line="推进主线",
            goals_json='["推进主线","保留悬念"]',
        )
        context = broker.build_chapter_context(FakeRepo(), "project-1", chapter_plan)

        self.assertEqual(context.project_id, "project-1")
        self.assertLessEqual(len(context.active_entities), 2)
        self.assertLessEqual(len(context.active_threads), 1)
        self.assertLessEqual(len(context.previous_chapter_summaries), 2)
        self.assertTrue(all(rel.source_name != "丙" for rel in context.active_relations))

    def test_artifact_store_namespaces_by_project(self) -> None:
        with TemporaryDirectory() as tmp:
            store = ArtifactStore(tmp)
            writer_output = WriterOutput(
                chapter_number=1,
                title="第一章",
                body="正文内容",
                char_count=4,
                end_of_chapter_summary="摘要",
                generation_meta={"mode": "scene"},
            )
            paths = store.save_writer_output("project-xyz", 1, writer_output)

            self.assertIn("projects/project-xyz/chapters/1", paths["draft_blob_path"])
            self.assertTrue(Path(paths["draft_blob_path"]).exists())
            self.assertTrue(Path(paths["meta_path"]).exists())

    def test_artifact_store_persists_scene_blobs(self) -> None:
        with TemporaryDirectory() as tmp:
            store = ArtifactStore(tmp)
            writer_output = WriterOutput(
                project_id="project-xyz",
                chapter_number=2,
                title="第二章",
                body="拼接正文",
                char_count=4,
                end_of_chapter_summary="摘要",
                scene_outputs=[
                    SceneOutput(
                        scene_no=1,
                        scene_objective="相遇",
                        text="scene body",
                        micro_summary="scene summary",
                    )
                ],
            )

            paths = store.save_writer_output("project-xyz", 2, writer_output)
            stored_output = paths["writer_output"]

            self.assertEqual(len(stored_output.scene_outputs), 1)
            self.assertTrue(stored_output.scene_outputs[0].text_blob_path)
            self.assertTrue(Path(stored_output.scene_outputs[0].text_blob_path).exists())

    def test_retrieval_broker_includes_retrieved_memories(self) -> None:
        class FakeRepo:
            pass

        class FakeMemoryIndex:
            def search(self, *, project_id: str, query: str, limit: int = 3):
                return [
                    MemorySnippet(
                        chapter_number=1,
                        title="旧章",
                        summary="主角在车站收到警告",
                        excerpt="别去看站台上的第三个人",
                        score=0.91,
                    )
                ]

        chapter_plan = SimpleNamespace(
            chapter_number=2,
            title="第二章",
            one_line="继续追查",
            goals_json='["追查来源"]',
        )
        broker = RetrievalBroker(
            context_budget_chars=1000,
            max_threads=2,
            memory_index=FakeMemoryIndex(),
        )

        with patch("forwin.retrieval.broker.assemble_context") as mocked:
            mocked.return_value = ChapterContextPack(
                project_id="p1",
                project_title="书",
                genre="悬疑",
                premise="前提",
                setting_summary="设定",
                chapter_number=2,
                chapter_plan_title="第二章",
                chapter_plan_one_line="继续追查",
                chapter_goals=["追查来源"],
                previous_chapter_summaries=["前章摘要"],
                active_entities=[],
                active_relations=[],
                active_threads=[],
                timeline=None,
            )
            pack = broker.build_chapter_context(FakeRepo(), "p1", chapter_plan)

        self.assertEqual(len(pack.retrieved_memories), 1)
        self.assertEqual(pack.retrieved_memories[0].title, "旧章")

    def test_create_memory_index_supports_remote_embedder(self) -> None:
        with TemporaryDirectory() as tmp:
            index = create_memory_index(
                backend="local",
                root_dir=tmp,
                embedding_backend="remote",
                embedding_base_url="https://embed.example/v1",
                embedding_api_key="sk-test",
                embedding_model="embed-model",
                embedding_dims=128,
            )

            self.assertIsInstance(index.embedder, RemoteTextEmbedder)
            self.assertEqual(index.embedder.dims, 128)

    def test_scene_writer_flow(self) -> None:
        class FakeLLMClient:
            def __init__(self) -> None:
                self.responses = [
                    '{"scenes":[{"scene_no":1,"objective":"相遇","must_progress_points":["主角遇敌"],"time_hint":"清晨","location_hint":"荒原","involved_entities":["林夜"],"micro_hook":"敌人逼近","target_chars":700},{"scene_no":2,"objective":"逃离","must_progress_points":["主角脱身"],"time_hint":"午后","location_hint":"峡谷","involved_entities":["林夜"],"micro_hook":"更大危机","target_chars":700}]}',
                    '{"text":"scene one","micro_summary":"相遇总结","scene_time_point":"清晨","scene_location_id":"荒原","involved_entities":["林夜"]}',
                    '{"text":"scene two","micro_summary":"逃离总结","scene_time_point":"午后","scene_location_id":"峡谷","involved_entities":["林夜"]}',
                    '{"title":"拼接章","body":"scene one\\nscene two","end_of_chapter_summary":"本章完成逃离"}',
                    '{"state_changes":[{"entity_name":"林夜","entity_kind":"character","field":"location","old_value":"荒原","new_value":"峡谷","reason":"逃离成功"}],"new_events":[{"summary":"林夜逃离","significance":"major","involved_entity_names":["林夜"],"roles":["protagonist"]}],"thread_beats":[{"thread_name":"主线","beat_type":"escalation","description":"危机升级"}],"time_advance":{"new_time_label":"第二天","duration_description":"半日后"}}',
                ]

            def chat(self, messages, temperature: float, max_tokens: int) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(
            FakeLLMClient(),
            writer_mode="scene",
            default_scene_count=2,
            max_scene_count=3,
        )
        context = SimpleNamespace(
            project_title="书",
            premise="前提",
            genre="玄幻",
            setting_summary="背景",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="相遇并逃离",
            chapter_goals=["相遇", "逃离"],
            previous_chapter_summaries=[],
            active_entities=[],
            active_relations=[],
            active_threads=[],
            timeline=None,
        )

        output = writer.write_chapter(context)

        self.assertEqual(output.title, "拼接章")
        self.assertEqual(len(output.scene_outputs), 2)
        self.assertEqual(output.generation_meta["mode"], "scene")
        self.assertEqual(output.generation_meta["call_count"], 5)
        self.assertEqual(output.state_changes[0].field, "location")

    def test_scene_writer_falls_back_when_scene_breakdown_json_fails(self) -> None:
        class FakeLLMClient:
            def __init__(self) -> None:
                self.responses = [
                    "scene breakdown unavailable",
                    '{"text":"scene one","micro_summary":"相遇总结","scene_time_point":"清晨","scene_location_id":"荒原","involved_entities":["林夜"]}',
                    '{"text":"scene two","micro_summary":"推进总结","scene_time_point":"午后","scene_location_id":"旧站台","involved_entities":["林夜"]}',
                    '{"title":"回退章","body":"scene one\\nscene two","end_of_chapter_summary":"本章完成推进"}',
                    '{"state_changes":[],"new_events":[],"thread_beats":[]}',
                ]

            def chat(self, messages, temperature: float, max_tokens: int, response_format=None) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(
            FakeLLMClient(),
            writer_mode="scene",
            default_scene_count=2,
            max_scene_count=3,
        )
        context = SimpleNamespace(
            project_id="p1",
            project_title="书",
            premise="前提",
            genre="玄幻",
            setting_summary="背景",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="相遇并推进",
            chapter_goals=["相遇", "推进"],
            previous_chapter_summaries=[],
            active_entities=[],
            active_relations=[],
            active_threads=[],
            timeline=None,
        )

        output = writer.write_chapter(context)

        self.assertEqual(output.title, "回退章")
        self.assertEqual(len(output.scene_outputs), 2)
        self.assertEqual(
            [scene.scene_objective for scene in output.scene_outputs],
            ["相遇", "推进"],
        )

    def test_scene_writer_falls_back_to_single_when_scene_generation_times_out(self) -> None:
        class FakeLLMClient:
            def __init__(self) -> None:
                self.responses = [
                    '{"scenes":[{"scene_no":1,"objective":"相遇","must_progress_points":["主角遇敌"],"target_chars":700}]}',
                    httpx.ReadTimeout("scene timed out"),
                    httpx.ReadTimeout("scene timed out again"),
                    '{"title":"单章回退","body":"主角在暴雨夜踏入站台，听见远处传来列车进站的怪响。","end_of_chapter_summary":"主角抵达异变现场"}',
                    '{"state_changes":[],"new_events":[],"thread_beats":[]}',
                ]

            def chat(self, messages, temperature: float, max_tokens: int, **kwargs) -> str:
                item = self.responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        writer = ChapterWriter(
            FakeLLMClient(),
            writer_mode="scene",
            default_scene_count=2,
            max_scene_count=3,
        )
        context = SimpleNamespace(
            project_id="p1",
            project_title="书",
            premise="前提",
            genre="玄幻",
            setting_summary="背景",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="相遇并推进",
            chapter_goals=["相遇", "推进"],
            previous_chapter_summaries=[],
            active_entities=[],
            active_relations=[],
            active_threads=[],
            timeline=None,
        )

        output = writer.write_chapter(context)

        self.assertEqual(output.title, "单章回退")
        self.assertEqual(output.generation_meta["mode"], "single")
        self.assertTrue(output.generation_meta["fallback_from_scene"])
        self.assertEqual(len(output.scene_outputs), 0)

    def _make_publisher_manager(self) -> tuple[TemporaryDirectory, object, PublisherManager]:
        tmp = TemporaryDirectory()
        db_path = str(Path(tmp.name) / "publisher.db")
        engine = get_engine(db_path)
        init_db(engine)
        session_factory = get_session_factory(engine)
        return tmp, engine, PublisherManager(session_factory, extension_api_key="secret")

    def test_publisher_manager_tracks_extension_heartbeat_and_stale_state(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertFalse(items["qidian"]["connected"])
            self.assertFalse(items["qidian"]["extension_online"])
            self.assertEqual(items["qidian"]["supported_login_methods"], ["scan"])

            manager.record_extension_heartbeat(
                client_id="client-1",
                extension_version="0.1.0",
                browser_name="Chrome",
                browser_version="123.0",
                backend_base_url="http://192.168.31.10:8899",
                platforms=[
                    {
                        "platform": "qidian",
                        "connected": True,
                        "login_method": "scan",
                        "last_error": "",
                    },
                    {
                        "platform": "fanqie",
                        "connected": False,
                        "login_method": "scan",
                        "last_error": "等待扫码",
                    },
                ],
            )

            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertTrue(items["qidian"]["connected"])
            self.assertTrue(items["qidian"]["extension_online"])
            self.assertEqual(items["fanqie"]["last_error"], "等待扫码")

            with manager.session_factory() as session:
                client = session.get(PublisherExtensionClient, "client-1")
                client.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=5)
                session.commit()

            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertFalse(items["qidian"]["extension_online"])
            self.assertTrue(items["qidian"]["connected"])

            manager.record_browser_session(
                client_id="client-1",
                platform="qidian",
                cookies=[
                    {
                        "name": "AppAuthToken",
                        "value": "token",
                        "domain": ".write.qq.com",
                        "path": "/",
                    },
                    {
                        "name": "pubtoken",
                        "value": "pub",
                        "domain": ".write.qq.com",
                        "path": "/",
                    },
                ],
            )
            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertTrue(items["qidian"]["connected"])
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_upload_jobs_and_comment_upsert(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            job = manager.create_upload_job(
                platform="qidian",
                book_name="测试书",
                chapter_title="第一章",
                body="正文",
                upload_url=None,
                publish=True,
            )
            self.assertEqual(job["status"], "pending")

            claimed = manager.claim_next_upload_job(
                client_id="client-1",
                connected_platforms=["qidian"],
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["status"], "running")
            self.assertEqual(claimed["extension_client_id"], "client-1")

            running = manager.update_upload_job_result(
                job_id=job["job_id"],
                client_id="client-1",
                status="running",
                message="扩展已接管任务",
                current_url="https://write.qq.com/portal/dashboard",
                error="",
                result_payload={"phase": "claimed"},
            )
            self.assertEqual(running["status"], "running")

            completed = manager.update_upload_job_result(
                job_id=job["job_id"],
                client_id="client-1",
                status="succeeded",
                message="发布成功",
                current_url="https://write.qq.com/portal/dashboard",
                error="",
                result_payload={"mode": "publish"},
            )
            self.assertEqual(completed["status"], "succeeded")

            sync_job = manager.create_comment_sync_job(
                platform="fanqie",
                work_id="book-1",
                work_name="测试书",
                chapter_id="chapter-1",
                chapter_title="第一章",
                limit=50,
            )
            self.assertEqual(sync_job["status"], "pending")

            first_batch = manager.ingest_comments_batch(
                client_id="client-1",
                platform="fanqie",
                job_id=sync_job["job_id"],
                comments=[
                    {
                        "remote_comment_id": "comment-1",
                        "work_id": "book-1",
                        "work_name": "测试书",
                        "chapter_id": "chapter-1",
                        "chapter_title": "第一章",
                        "author_id": "user-1",
                        "author_name": "读者A",
                        "body": "催更",
                        "created_at": "2026-03-31T12:00:00Z",
                        "raw_payload": {"body": "催更"},
                    }
                ],
            )
            second_batch = manager.ingest_comments_batch(
                client_id="client-1",
                platform="fanqie",
                comments=[
                    {
                        "remote_comment_id": "comment-1",
                        "work_id": "book-1",
                        "work_name": "测试书",
                        "chapter_id": "chapter-1",
                        "chapter_title": "第一章",
                        "author_id": "user-1",
                        "author_name": "读者A",
                        "body": "二刷催更",
                        "created_at": "2026-03-31T12:01:00Z",
                        "raw_payload": {"body": "二刷催更"},
                    }
                ],
            )

            self.assertEqual(first_batch["inserted"], 1)
            self.assertEqual(second_batch["updated"], 1)

            with manager.session_factory() as session:
                comment_count = session.execute(select(func.count(PublisherRawComment.id))).scalar_one()
            self.assertEqual(comment_count, 1)

            session_sync = manager.record_browser_session(
                client_id="client-1",
                platform="qidian",
                cookies=[
                    {
                        "name": "AppAuthToken",
                        "value": "token-value",
                        "domain": ".write.qq.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "no_restriction",
                        "expirationDate": 1893456000,
                    },
                    {
                        "name": "pubtoken",
                        "value": "cookie-value",
                        "domain": ".write.qq.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "no_restriction",
                        "expirationDate": 1893456000,
                    }
                ],
            )
            self.assertTrue(session_sync["ok"])
            self.assertTrue(manager.has_browser_session("qidian"))
            stored = manager.get_browser_session("qidian")
            assert stored is not None
            self.assertEqual(stored["cookie_count"], 2)
            self.assertEqual(stored["cookies"][0]["sameSite"], "None")

            server_job = manager.create_upload_job(
                platform="qidian",
                book_name="测试书",
                chapter_title="后端接管章节",
                body="正文",
                upload_url=None,
                publish=False,
            )
            claimed = manager.claim_upload_job_for_server(
                job_id=server_job["job_id"],
                client_id=stored["client_id"],
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["status"], "running")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_requeues_running_jobs_after_restart(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            job = manager.create_upload_job(
                platform="fanqie",
                book_name="测试书",
                chapter_title="重试章节",
                body="正文",
                upload_url=None,
                publish=False,
            )
            manager.update_upload_job_result(
                job_id=job["job_id"],
                client_id="client-1",
                status="running",
                message="后端接管中",
                current_url="https://fanqienovel.com/main/writer/",
                error="",
                result_payload={"phase": "starting"},
            )

            recovered_platforms = manager.requeue_interrupted_upload_jobs()
            self.assertEqual(recovered_platforms, ["fanqie"])

            with manager.session_factory() as session:
                stored_job = session.get(PublisherUploadJob, job["job_id"])
                assert stored_job is not None
                self.assertEqual(stored_job.status, "pending")
                self.assertIsNone(stored_job.started_at)
                self.assertEqual(stored_job.extension_client_id, "")
                self.assertEqual(stored_job.current_url, "")
                self.assertEqual(stored_job.result_message, "服务重启后，上传任务已重新排队。")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publishers_page_and_extension_api_routes(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        old_manager = api_module._publisher_manager
        try:
            with TestClient(api_module.app) as client:
                api_module._publisher_manager = manager

                page = client.get("/publishers")
                self.assertEqual(page.status_code, 200)
                self.assertIn("浏览器扩展", page.text)
                self.assertIn("如果你是用 macOS 浏览器访问这台 Linux 后端", page.text)
                self.assertIn("browser_extension/forwin-publisher", page.text)
                self.assertIn("execute-upload-job", page.text)
                self.assertIn("/api/publishers/extension-package", page.text)

                package = client.get("/api/publishers/extension-package")
                self.assertEqual(package.status_code, 200)
                self.assertEqual(package.headers["content-type"], "application/zip")
                self.assertIn("forwin-publisher-extension.zip", package.headers["content-disposition"])
                self.assertGreater(len(package.content), 0)
                self.assertIn(b"manifest.json", package.content)

                platforms = client.get("/api/publishers/platforms")
                self.assertEqual(platforms.status_code, 200)
                self.assertEqual(len(platforms.json()), 2)
                self.assertEqual(platforms.json()[0]["supported_login_methods"], ["scan"])

                with patch.object(api_module, "_start_backend_upload_thread", lambda _job_id: None):
                    created = client.post(
                        "/api/publishers/upload-jobs",
                        json={
                            "platform": "fanqie",
                            "book_name": "测试书",
                            "chapter_title": "第一章",
                            "body": "正文",
                            "publish": False,
                        },
                    )
                self.assertEqual(created.status_code, 200)
                job_id = created.json()["job_id"]
                self.assertEqual(created.json()["status"], "pending")

                fetched = client.get(f"/api/publishers/upload-jobs/{job_id}")
                self.assertEqual(fetched.status_code, 200)
                self.assertEqual(fetched.json()["job_id"], job_id)

                unauthorized = client.post(
                    "/api/publishers/extension/heartbeat",
                    json={
                        "client_id": "client-1",
                        "platforms": [],
                    },
                )
                self.assertEqual(unauthorized.status_code, 401)

                heartbeat = client.post(
                    "/api/publishers/extension/heartbeat",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "extension_version": "0.1.0",
                        "browser_name": "Chrome",
                        "browser_version": "123.0",
                        "backend_base_url": "http://192.168.31.10:8899",
                        "platforms": [
                            {
                                "platform": "fanqie",
                                "connected": True,
                                "login_method": "scan",
                                "cookies": [
                                    {
                                        "name": "sessionid",
                                        "value": "cookie-value",
                                        "domain": ".fanqienovel.com",
                                        "path": "/",
                                        "secure": True,
                                        "httpOnly": True,
                                        "sameSite": "lax",
                                    }
                                ],
                                "raw_state": {"source": "test"},
                            }
                        ],
                    },
                )
                self.assertEqual(heartbeat.status_code, 200)
                self.assertTrue(heartbeat.json()["ok"])

                with manager.session_factory() as session:
                    stored_session = session.get(PublisherBrowserSession, "fanqie")
                    self.assertIsNone(stored_session)

                session_sync = client.post(
                    "/api/publishers/extension/session-sync",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "platform": "qidian",
                        "cookies": [
                            {
                                "name": "AppAuthToken",
                                "value": "token-value",
                                "domain": ".write.qq.com",
                                "path": "/",
                                "secure": True,
                                "httpOnly": True,
                                "sameSite": "no_restriction",
                                "expirationDate": 1893456000,
                            },
                            {
                                "name": "pubtoken",
                                "value": "cookie-value",
                                "domain": ".write.qq.com",
                                "path": "/",
                                "secure": True,
                                "httpOnly": True,
                                "sameSite": "no_restriction",
                                "expirationDate": 1893456000,
                            }
                        ],
                    },
                )
                self.assertEqual(session_sync.status_code, 200)
                self.assertEqual(session_sync.json()["cookie_count"], 2)

                with patch.object(api_module, "_start_backend_upload_thread", lambda _job_id: None):
                    server_claim = client.post(
                        "/api/publishers/upload-jobs",
                        json={
                            "platform": "qidian",
                            "book_name": "测试书",
                            "chapter_title": "后端接管章节",
                            "body": "正文",
                            "publish": False,
                        },
                    )
                self.assertEqual(server_claim.status_code, 200)
                self.assertEqual(server_claim.json()["status"], "running")
                self.assertEqual(server_claim.json()["message"], "后端正在使用已同步会话执行上传。")

                updated = client.post(
                    f"/api/publishers/upload-jobs/{job_id}/result",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "status": "succeeded",
                        "message": "保存完成",
                        "current_url": "https://fanqienovel.com/main/writer/",
                        "result_payload": {"mode": "draft"},
                    },
                )
                self.assertEqual(updated.status_code, 200)
                self.assertEqual(updated.json()["status"], "succeeded")

                claimed_none = client.post(
                    "/api/publishers/extension/upload-jobs/claim",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "connected_platforms": ["fanqie"],
                    },
                )
                self.assertEqual(claimed_none.status_code, 200)
                self.assertFalse(claimed_none.json()["found"])

                comment_job = client.post(
                    "/api/publishers/comment-sync-jobs",
                    json={
                        "platform": "fanqie",
                        "work_id": "book-1",
                        "work_name": "测试书",
                    },
                )
                self.assertEqual(comment_job.status_code, 200)

                comments = client.post(
                    "/api/publishers/extension/comments/batch",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "platform": "fanqie",
                        "job_id": comment_job.json()["job_id"],
                        "comments": [
                            {
                                "remote_comment_id": "comment-1",
                                "work_id": "book-1",
                                "work_name": "测试书",
                                "body": "催更",
                                "raw_payload": {"body": "催更"},
                            }
                        ],
                    },
                )
                self.assertEqual(comments.status_code, 200)
                self.assertEqual(comments.json()["inserted"], 1)

                self.assertEqual(client.get("/publishers/qidian/auth").status_code, 404)
                self.assertEqual(client.post("/api/publishers/fanqie/login").status_code, 404)
                self.assertEqual(client.post("/api/publishers/upload", json={}).status_code, 404)
        finally:
            api_module._publisher_manager = old_manager
            engine.dispose()
            tmp.cleanup()

    def test_home_page_renders_minimax_defaults_and_apikey_field(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                with TestClient(api_module.app) as client:
                    api_module._config = Config(
                        db_path=":memory:",
                        runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                        minimax_api_key="",
                        minimax_base_url="https://api.minimaxi.com/v1",
                        minimax_model="MiniMax-M2.7",
                    )
                    api_module._runtime_settings = RuntimeSettingsStore(
                        api_module._config.runtime_settings_path,
                        default_api_key="",
                        default_base_url=api_module._config.minimax_base_url,
                        default_model=api_module._config.minimax_model,
                    )

                    page = client.get("/")

                    self.assertEqual(page.status_code, 200)
                    self.assertIn('id="api_key"', page.text)
                    self.assertIn("https://api.minimaxi.com/v1", page.text)
                    self.assertIn("MiniMax-M2.7", page.text)
                    self.assertIn("黑箱模式", page.text)
                    self.assertIn("检查点模式：写完初稿和 review 后暂停", page.text)
                    self.assertIn("当前任务是按运行模式主动暂停", page.text)
                    self.assertIn("/publishers", page.text)
                    self.assertIn("保存默认配置", page.text)
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_publishers_page_uses_extension_bridge_flow(self) -> None:
        with TestClient(api_module.app) as client:
            page = client.get("/publishers")

        self.assertEqual(page.status_code, 200)
        self.assertIn("forwin-publisher-extension", page.text)
        self.assertIn("open-login", page.text)
        self.assertIn("execute-upload-job", page.text)
        self.assertIn("浏览器扩展未响应", page.text)

    def test_review_endpoints_expose_and_continue_paused_chapter(self) -> None:
        class FakeThread:
            created: list["FakeThread"] = []

            def __init__(self, target=None, args=None, daemon=None):
                self.target = target
                self.args = args or ()
                self.daemon = daemon
                self.started = False
                FakeThread.created.append(self)

            def start(self):
                self.started = True

        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "review_api.db")
            orchestrator = WritingOrchestrator(
                Config(
                    db_path=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="checkpoint",
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "Review API",
                        "setting_summary": "无",
                        "chapters": [
                            {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]},
                            {"chapter_number": 2, "title": "第二章", "one_line": "延续", "goals": ["继续推进"]},
                        ],
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = f"第{context.chapter_number}章正文" * 300
                    return WriterOutput(
                        chapter_number=context.chapter_number,
                        title=f"第{context.chapter_number}章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter
                run_result = orchestrator.run("p", "g", 2)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(run_result.status, "needs_review")

            old_config = api_module._config
            old_store = api_module._runtime_settings
            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            old_orchestrator = api_module._orchestrator
            old_tasks = api_module._tasks
            temp_engine = None
            try:
                api_module._config = Config(
                    db_path=db_path,
                    runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                    minimax_api_key="",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                    operation_mode="blackbox",
                )
                temp_engine = get_engine(db_path)
                api_module._engine = temp_engine
                api_module._SessionFactory = get_session_factory(api_module._engine)
                api_module._orchestrator = orchestrator
                api_module._runtime_settings = RuntimeSettingsStore(
                    api_module._config.runtime_settings_path,
                    default_api_key="",
                    default_base_url=api_module._config.minimax_base_url,
                    default_model=api_module._config.minimax_model,
                    default_operation_mode="blackbox",
                    default_freeze_failed_candidates=True,
                )
                api_module._tasks = {}
                with patch.object(api_module.threading, "Thread", FakeThread):
                    review_payload = api_module.get_chapter_review(
                        run_result.project_id, 1
                    )
                    approve_payload = api_module.approve_chapter_review(
                        run_result.project_id,
                        1,
                        api_module.ChapterReviewApproveRequest(continue_generation=True),
                    )
                    task_payload = api_module.get_task(approve_payload.task_id)

                self.assertEqual(review_payload.verdict, "pass")
                self.assertTrue(approve_payload.task_id)
                self.assertIn("已启动后续章节继续执行", approve_payload.message)
                self.assertEqual(task_payload.project_id, run_result.project_id)
                self.assertEqual(len(FakeThread.created), 1)
                self.assertTrue(FakeThread.created[0].started)
            finally:
                if temp_engine is not None:
                    temp_engine.dispose()
                api_module._config = old_config
                api_module._runtime_settings = old_store
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory
                api_module._orchestrator = old_orchestrator
                api_module._tasks = old_tasks

    def test_generate_accepts_request_level_minimax_overrides(self) -> None:
        class FakeThread:
            created: list["FakeThread"] = []

            def __init__(self, target=None, args=None, daemon=None):
                self.target = target
                self.args = args or ()
                self.daemon = daemon
                self.started = False
                FakeThread.created.append(self)

            def start(self):
                self.started = True

        with TestClient(api_module.app) as client:
            old_config = api_module._config
            old_tasks = api_module._tasks
            try:
                api_module._config = Config(
                    db_path=":memory:",
                    minimax_api_key="",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                api_module._tasks = {}

                with patch.object(api_module.threading, "Thread", FakeThread):
                    response = client.post(
                        "/api/generate",
                        json={
                            "premise": "一段前提",
                            "genre": "玄幻",
                            "num_chapters": 2,
                            "api_key": "sk-inline",
                            "base_url": "https://example.test/v1",
                            "model": "custom-model",
                        },
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(FakeThread.created), 1)
                thread = FakeThread.created[0]
                self.assertTrue(thread.started)
                self.assertIs(thread.target, api_module._run_generation_with_config)
                runtime_config = thread.args[4]
                self.assertEqual(runtime_config.minimax_api_key, "sk-inline")
                self.assertEqual(runtime_config.minimax_base_url, "https://example.test/v1")
                self.assertEqual(runtime_config.minimax_model, "custom-model")
            finally:
                api_module._config = old_config
                api_module._tasks = old_tasks

    def test_approve_review_reuses_existing_orchestrator(self) -> None:
        class FakeOrchestrator:
            def __init__(self):
                self.calls = []

            def accept_review(self, project_id: str, chapter_number: int) -> dict[str, str]:
                self.calls.append((project_id, chapter_number))
                return {"message": "ok", "frozen_artifact": ""}

        old_config = api_module._config
        old_orchestrator = api_module._orchestrator
        old_tasks = api_module._tasks
        old_store = api_module._runtime_settings
        try:
            api_module._config = Config(
                db_path=":memory:",
                runtime_settings_path="data/runtime_settings.json",
                minimax_api_key="",
                minimax_base_url="https://api.minimaxi.com/v1",
                minimax_model="MiniMax-M2.7",
            )
            api_module._runtime_settings = RuntimeSettingsStore(
                "data/runtime_settings.json",
                default_api_key="",
                default_base_url=api_module._config.minimax_base_url,
                default_model=api_module._config.minimax_model,
                default_operation_mode="blackbox",
                default_freeze_failed_candidates=True,
            )
            api_module._tasks = {}
            fake = FakeOrchestrator()
            api_module._orchestrator = fake
            with patch.object(api_module, "WritingOrchestrator", side_effect=AssertionError("should not instantiate")):
                payload = api_module.approve_chapter_review(
                    "proj-1",
                    1,
                    api_module.ChapterReviewApproveRequest(continue_generation=False),
                )

            self.assertTrue(payload.ok)
            self.assertEqual(fake.calls, [("proj-1", 1)])
        finally:
            api_module._config = old_config
            api_module._orchestrator = old_orchestrator
            api_module._tasks = old_tasks
            api_module._runtime_settings = old_store

    def test_sqlite_engine_enables_foreign_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "fk.db")
            engine = get_engine(db_path)
            try:
                with engine.connect() as conn:
                    foreign_keys = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
            finally:
                engine.dispose()

        self.assertEqual(foreign_keys, 1)

    def test_llm_settings_api_persists_runtime_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                with TestClient(api_module.app) as client:
                    api_module._config = Config(
                        db_path=":memory:",
                        runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                        minimax_api_key="",
                        minimax_base_url="https://api.minimaxi.com/v1",
                        minimax_model="MiniMax-M2.7",
                    )
                    api_module._runtime_settings = RuntimeSettingsStore(
                        api_module._config.runtime_settings_path,
                        default_api_key="",
                        default_base_url=api_module._config.minimax_base_url,
                        default_model=api_module._config.minimax_model,
                    )
                    saved = client.post(
                        "/api/settings/llm",
                        json={
                            "api_key": "sk-saved",
                            "base_url": "https://example.saved/v1",
                            "model": "saved-model",
                        },
                    )
                    current = client.get("/api/settings/llm")

                self.assertEqual(saved.status_code, 200)
                self.assertEqual(current.status_code, 200)
                self.assertEqual(current.json()["has_api_key"], True)
                self.assertEqual(current.json()["base_url"], "https://example.saved/v1")
                self.assertEqual(current.json()["model"], "saved-model")
                self.assertEqual(current.json()["operation_mode"], "blackbox")
                self.assertEqual(current.json()["freeze_failed_candidates"], True)
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_llm_settings_api_persists_runtime_modes(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                with TestClient(api_module.app) as client:
                    api_module._config = Config(
                        db_path=":memory:",
                        runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                        minimax_api_key="",
                        minimax_base_url="https://api.minimaxi.com/v1",
                        minimax_model="MiniMax-M2.7",
                    )
                    api_module._runtime_settings = RuntimeSettingsStore(
                        api_module._config.runtime_settings_path,
                        default_api_key="",
                        default_base_url=api_module._config.minimax_base_url,
                        default_model=api_module._config.minimax_model,
                    )
                    saved = client.post(
                        "/api/settings/llm",
                        json={
                            "api_key": "",
                            "base_url": "https://api.minimaxi.com/v1",
                            "model": "MiniMax-M2.7",
                            "operation_mode": "checkpoint",
                            "freeze_failed_candidates": False,
                        },
                    )
                    current = client.get("/api/settings/llm")

                self.assertEqual(saved.status_code, 200)
                self.assertEqual(current.status_code, 200)
                self.assertEqual(current.json()["operation_mode"], "checkpoint")
                self.assertEqual(current.json()["freeze_failed_candidates"], False)
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_generate_uses_saved_runtime_settings_when_request_omits_key(self) -> None:
        class FakeThread:
            created: list["FakeThread"] = []

            def __init__(self, target=None, args=None, daemon=None):
                self.target = target
                self.args = args or ()
                self.daemon = daemon
                self.started = False
                FakeThread.created.append(self)

            def start(self):
                self.started = True

        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            old_tasks = api_module._tasks
            try:
                with TestClient(api_module.app) as client:
                    api_module._config = Config(
                        db_path=":memory:",
                        runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                        minimax_api_key="",
                        minimax_base_url="https://api.minimaxi.com/v1",
                        minimax_model="MiniMax-M2.7",
                    )
                    api_module._runtime_settings = RuntimeSettingsStore(
                        api_module._config.runtime_settings_path,
                        default_api_key="",
                        default_base_url=api_module._config.minimax_base_url,
                        default_model=api_module._config.minimax_model,
                    )
                    api_module._runtime_settings.save(
                        api_key="sk-from-store",
                        base_url="https://stored.example/v1",
                        model="stored-model",
                    )
                    api_module._tasks = {}
                    with patch.object(api_module.threading, "Thread", FakeThread):
                        response = client.post(
                            "/api/generate",
                            json={
                                "premise": "一段前提",
                                "genre": "玄幻",
                                "num_chapters": 1,
                                "api_key": "",
                                "base_url": "",
                                "model": "",
                            },
                        )

                self.assertEqual(response.status_code, 200)
                runtime_config = FakeThread.created[0].args[4]
                self.assertEqual(runtime_config.minimax_api_key, "sk-from-store")
                self.assertEqual(runtime_config.minimax_base_url, "https://stored.example/v1")
                self.assertEqual(runtime_config.minimax_model, "stored-model")
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store
                api_module._tasks = old_tasks


if __name__ == "__main__":
    unittest.main()
