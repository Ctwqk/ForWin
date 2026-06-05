from __future__ import annotations

import asyncio
import gc
import json
import logging
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import fastapi.routing
import starlette.concurrency
import starlette.responses
from sqlalchemy import func, select, text
from sqlalchemy.orm import close_all_sessions
from tests_support import capture_select_statements, count_matching_statements

import forwin.api as api_module
import forwin.api_project_payloads as api_project_payloads_module
import forwin.api_publisher_ops as api_publisher_ops_module
import forwin.api_runtime as api_runtime_module
from forwin import cli as cli_module
from forwin.api_pages import render_home_page
from forwin.config import Config
from forwin.director import ArcDirector
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.entity import EntityState
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.event import CanonEvent, EventEntityLink
from forwin.models.governance import BandCheckpoint, DecisionEvent
from forwin.models.phase import (
    ArcEnvelope,
    ArcEnvelopeAnalysis,
    ArcStructureDraft,
    BandExperiencePlan,
    ChapterRewriteAttempt,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalChapterLedger,
    ProvisionalBandExecution,
    ProvisionalPromotionRecord,
)
from forwin.models.phase4 import NPCIntentSnapshot, WorldSimulationTurn
from forwin.models.world_v4 import ScenarioRehearsalRunRow
from forwin.models.publisher import (
    PublisherCommentSyncJob,
    PublisherBrowserSession,
    PublisherBrowserSessionEntry,
    PublisherConnectionState,
    PublisherExtensionClient,
    PublisherExtensionPlatformState,
    PublisherRawComment,
    PublisherUploadJob,
)
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.orchestrator.loop import RunResult, WritingOrchestrator
from forwin.orchestrator.phase3 import PacingAssessment, PacingStrategist, ReplanGovernor, StageAssessment
from forwin.orchestrator.phase24 import ArcEnvelopeManager, PlanningServices
from forwin.orchestrator.phase24 import policy_for_total_chapters
from forwin.orchestrator.phase4 import NPCIntentGenerator, WorldSimulator
from forwin.orchestrator.thread_sampling import sample_active_threads
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.reviewer.hub import HistoricalReviewHub
from forwin.protocol.context import (
    ChapterContextPack,
    ArcEnvelopeView,
    AudienceHintView,
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
from forwin.retrieval.memory_index import QdrantChapterMemoryIndex, RemoteTextEmbedder, create_memory_index
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore
from forwin.utils import LLMJSONParseError, parse_llm_json
from tests.qdrant import FakeQdrantClient, FakeQdrantModels
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.prompts import (
    build_preview_chapter_prompt,
    build_scene_generation_prompt,
    build_single_chapter_draft_prompt,
)


class _TestClientResponse:
    __test__ = False

    def __init__(self, *, status_code: int, headers: dict[str, str], content: bytes) -> None:
        self.status_code = int(status_code)
        self.headers = dict(headers)
        self.content = bytes(content)
        charset = "utf-8"
        content_type = str(self.headers.get("content-type", ""))
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        self.text = self.content.decode(charset, errors="replace")

    def json(self) -> object:
        return json.loads(self.content.decode("utf-8"))


class TestClient:
    """Minimal sync client for Python 3.13 where Starlette TestClient blocks on AnyIO."""

    __test__ = False

    def __init__(self, app, base_url: str = "http://testserver") -> None:
        self.app = app
        self.base_url = base_url
        self._runner: asyncio.Runner | None = None
        self._lifespan_cm = None
        self._patches = []

    def __enter__(self) -> "TestClient":
        async def inline_run_in_threadpool(func, *args, **inner_kwargs):
            return func(*args, **inner_kwargs)

        async def inline_iterate_in_threadpool(iterator):
            for item in iterator:
                yield item

        self._patches = [
            patch.object(fastapi.routing, "run_in_threadpool", inline_run_in_threadpool),
            patch.object(starlette.concurrency, "run_in_threadpool", inline_run_in_threadpool),
            patch.object(starlette.responses, "iterate_in_threadpool", inline_iterate_in_threadpool),
            patch.object(api_module, "_start_automation_scheduler", lambda: None),
            patch.object(api_module, "_stop_automation_scheduler", lambda: None),
        ]
        for active_patch in self._patches:
            active_patch.start()
        self._runner = asyncio.Runner()
        self._lifespan_cm = self.app.router.lifespan_context(self.app)
        self._runner.run(self._lifespan_cm.__aenter__())
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self._runner is not None and self._lifespan_cm is not None:
                self._runner.run(self._lifespan_cm.__aexit__(exc_type, exc, tb))
        finally:
            if self._runner is not None:
                self._runner.close()
                self._runner = None
            self._lifespan_cm = None
            while self._patches:
                self._patches.pop().stop()
        return False

    def get(self, url: str, **kwargs) -> _TestClientResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> _TestClientResponse:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs) -> _TestClientResponse:
        async def run_request() -> _TestClientResponse:
            body = b""
            headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
            request_headers = kwargs.get("headers") or {}
            json_body = kwargs.get("json")
            content = kwargs.get("content")
            if json_body is not None:
                body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                headers.append((b"content-type", b"application/json"))
            elif content is not None:
                body = content if isinstance(content, bytes) else str(content).encode("utf-8")
            for key, value in request_headers.items():
                headers.append((str(key).lower().encode("utf-8"), str(value).encode("utf-8")))
            if body:
                headers.append((b"content-length", str(len(body)).encode("utf-8")))

            request_sent = False
            sent_messages: list[dict[str, object]] = []

            async def receive() -> dict[str, object]:
                nonlocal request_sent
                if request_sent:
                    return {"type": "http.disconnect"}
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message: dict[str, object]) -> None:
                sent_messages.append(message)

            request = httpx.Request(method.upper(), f"{self.base_url}{url}")
            raw_path = request.url.raw_path
            query_string = request.url.query if isinstance(request.url.query, bytes) else str(request.url.query).encode("utf-8")
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": method.upper(),
                "scheme": request.url.scheme,
                "path": request.url.path,
                "raw_path": raw_path,
                "query_string": query_string,
                "root_path": "",
                "headers": headers,
                "client": ("testclient", 50000),
                "server": (request.url.host or "testserver", request.url.port or 80),
                "state": {},
                "app": self.app,
            }

            await self.app(scope, receive, send)

            status_code = 500
            response_headers: dict[str, str] = {}
            chunks: list[bytes] = []
            for message in sent_messages:
                message_type = message.get("type")
                if message_type == "http.response.start":
                    status_code = int(message.get("status", 500))
                    response_headers = {
                        bytes(key).decode("utf-8").lower(): bytes(value).decode("utf-8")
                        for key, value in message.get("headers", [])
                    }
                elif message_type == "http.response.body":
                    chunks.append(bytes(message.get("body", b"")))
            return _TestClientResponse(
                status_code=status_code,
                headers=response_headers,
                content=b"".join(chunks),
            )

        if self._runner is None:
            raise RuntimeError("TestClient must be used as a context manager.")
        return self._runner.run(run_request())


class Phase05RegressionTests(unittest.TestCase):
    def test_server_uploader_stub_raises_archived_error(self) -> None:
        from forwin.publishers.server_uploader import ServerPublisherUploader

        with self.assertRaises(RuntimeError) as ctx:
            ServerPublisherUploader()

        self.assertIn("archived", str(ctx.exception).lower())
        self.assertIn("browser extension", str(ctx.exception).lower())

    def test_home_page_exposes_publish_action_label(self) -> None:
        html = render_home_page(
            has_api_key=False,
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            operation_mode="copilot",
            freeze_failed_candidates=False,
        )

        self.assertIn("发布到平台", html)

    def test_config_from_env_reads_shared_fields_once(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FORWIN_DB_PATH": "tmp/test.db",
                "FORWIN_DATABASE_URL": "postgresql+psycopg://forwin:forwin@localhost:5432/forwin",
                "TEMPERATURE": "0.42",
                "MAX_TOKENS": "4096",
            },
            clear=False,
        ):
            config = Config.from_env()

        self.assertEqual(
            config.database_url,
            "postgresql+psycopg://forwin:forwin@localhost:5432/forwin",
        )
        self.assertFalse(hasattr(config, "db_path"))
        self.assertEqual(config.temperature, 0.42)
        self.assertEqual(config.max_tokens, 4096)

    def test_planning_services_removed_legacy_constructor(self) -> None:
        self.assertFalse(hasattr(PlanningServices, "from_legacy_args"))

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

    def test_build_task_progress_changes_includes_project_created_fields(self) -> None:
        changes = api_runtime_module._build_task_progress_changes(
            "project_created",
            {
                "project_id": "project-1",
                "title": "测试项目",
                "requested_chapters": 3,
            },
            include_project_created=True,
        )

        self.assertEqual(changes["project_id"], "project-1")
        self.assertEqual(changes["title"], "测试项目")
        self.assertEqual(changes["message"], "项目已创建：测试项目")
        self.assertEqual(changes["requested_chapters"], 3)

    def test_build_task_progress_changes_reuses_stage_mapping_for_continue_tasks(self) -> None:
        changes = api_runtime_module._build_task_progress_changes(
            "stage_changed",
            {
                "stage": "terminating",
                "current_chapter": 2,
                "completed_chapters": [1],
            },
        )

        self.assertEqual(changes["current_stage"], "terminating")
        self.assertEqual(changes["status"], "terminating")
        self.assertEqual(changes["current_chapter"], 2)
        self.assertEqual(changes["completed_chapters"], [1])
        self.assertNotIn("title", changes)

    def test_task_paused_message_reports_canon_system_block(self) -> None:
        result = RunResult(
            project_id="p",
            requested_chapters=1,
            paused_chapters=[1],
            frozen_artifacts=["canon-quality-gate-blocked"],
            system_block_chapters=[1],
        )

        message = api_runtime_module._paused_chapters_message(result)

        self.assertEqual(message, "章节 1 遇到 canon system block，需处理系统阻断后重试")

    def test_task_paused_message_keeps_repair_wording_for_plain_review_pause(self) -> None:
        result = RunResult(
            project_id="p",
            requested_chapters=1,
            paused_chapters=[1],
        )

        message = api_runtime_module._paused_chapters_message(result)

        self.assertEqual(message, "质量门阻断，需自动修复或重试章节: 1")

    def test_project_task_center_items_batch_load_chapter_plans(self) -> None:
        tmp = TemporaryDirectory()
        engine = get_engine(postgres_test_url("task-center"))
        init_db(engine)
        session_factory = get_session_factory(engine)
        old_session_factory = api_module._SessionFactory
        with api_module._tasks_lock:
            old_tasks = dict(api_module._tasks)
            api_module._tasks.clear()

        try:
            with session_factory() as session:
                for index in range(3):
                    project = Project(
                        id=new_id(),
                        title=f"项目{index + 1}",
                        premise="测试前提",
                        genre="玄幻",
                        setting_summary="设定",
                    )
                    session.add(project)
                    session.commit()
                    arc = ArcPlanVersion(
                        id=new_id(),
                        project_id=project.id,
                        version=1,
                        arc_synopsis="弧线",
                        status="active",
                    )
                    session.add(arc)
                    session.commit()
                    session.add_all(
                        [
                            ChapterPlan(
                                id=new_id(),
                                project_id=project.id,
                                arc_plan_id=arc.id,
                                chapter_number=1,
                                title="第一章",
                                one_line="推进一",
                                goals_json='["g1"]',
                                status="accepted",
                            ),
                            ChapterPlan(
                                id=new_id(),
                                project_id=project.id,
                                arc_plan_id=arc.id,
                                chapter_number=2,
                                title="第二章",
                                one_line="推进二",
                                goals_json='["g2"]',
                                status="needs_review",
                            ),
                        ]
                    )
                    for event_index in range(15):
                        session.add(
                            DecisionEvent(
                                id=new_id(),
                                project_id=project.id,
                                event_type="test_event",
                                summary=f"事件 {event_index}",
                                created_at=datetime.now(timezone.utc) + timedelta(seconds=event_index),
                            )
                        )
                    session.commit()

            api_module._SessionFactory = session_factory
            with capture_select_statements(engine) as select_statements:
                items = api_module._list_project_backed_task_items(limit=10)

            self.assertEqual(len(items), 3)
            self.assertEqual(count_matching_statements(select_statements, " from projects"), 1)
            self.assertEqual(
                count_matching_statements(select_statements, " from chapter_plans"),
                1,
            )
            self.assertTrue(
                any("row_number()" in statement and "decision_events" in statement for statement in select_statements),
                select_statements,
            )
        finally:
            api_module._SessionFactory = old_session_factory
            with api_module._tasks_lock:
                api_module._tasks.clear()
                api_module._tasks.update(old_tasks)
            engine.dispose()
            tmp.cleanup()

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
        with TemporaryDirectory() as tmp:
            missing_env = str(Path(tmp) / "missing.env")
            with patch.dict(
                "os.environ", {"FORWIN_ENV_FILE": missing_env}, clear=True
            ):
                config = Config.from_env()

        self.assertEqual(config.writer_mode, "scene")

    def test_cli_read_initializes_empty_database(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("fresh-read")
            output = StringIO()
            args = SimpleNamespace(
                database_url=db_path,
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
            db_path = postgres_test_url("fresh-status")
            output = StringIO()
            args = SimpleNamespace(
                database_url=db_path,
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
            db_path = postgres_test_url("orchestrator")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
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
                                "goals": ["正文"],
                            },
                            {
                                "chapter_number": 2,
                                "title": "第二章",
                                "one_line": "二",
                                "goals": ["正文"],
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
                    body = "正文" * 1300
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
                    return '{"title":"第一章","body":"' + ("正文" * 1200) + '。","end_of_chapter_summary":"总结"}'
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

    def test_orchestrator_drops_unsupported_state_field_and_keeps_draft(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("dirty-state")
            orchestrator = WritingOrchestrator(
                Config(database_url=db_path, minimax_api_key="", minimax_model="fake-model", chapter_review_form_mode="off")
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
            db_path = postgres_test_url("checkpoint")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
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
            db_path = postgres_test_url("freeze")
            artifact_root = str(Path(tmp) / "artifacts")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    artifact_root=artifact_root,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
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
                                summary="路人闯入",
                                significance="major",
                                involved_entity_names=["路人"],
                                roles=["protagonist"],
                            )
                        ],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter
                orchestrator.review_hub.review = lambda **kwargs: ReviewVerdict(
                    verdict="pass",
                    issues=[],
                )
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
            db_path = postgres_test_url("filter-events")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
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
                                summary="路人闯入",
                                significance="major",
                                involved_entity_names=["路人"],
                                roles=["protagonist"],
                            )
                        ],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter
                orchestrator.review_hub.review = lambda **kwargs: ReviewVerdict(
                    verdict="pass",
                    issues=[],
                )

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
            self.assertFalse(result.frozen_artifacts)
            self.assertEqual(statuses, [(1, "accepted")])
            self.assertGreaterEqual(draft_count, 1)
            self.assertEqual(event_count, 0)

    def test_blackbox_hard_review_fail_reaches_manual_gate_after_three_repairs(self) -> None:
        class HardFailReviewHub:
            def __init__(self) -> None:
                self.calls = 0

            def review(self, **kwargs) -> ReviewVerdict:  # noqa: ANN003
                self.calls += 1
                return ReviewVerdict(
                    verdict="fail",
                    issues=[
                        ContinuityIssue(
                            rule_name="sub_world_unknown_named_entity",
                            severity="error",
                            description="命名角色「不存在的人」未在当前 chapter 的 subworld 准入名单中。",
                            reviewer="continuity",
                            issue_type="subworld_admission",
                            target_scope="chapter",
                            evidence_refs=["chapter=1", "entity=不存在的人"],
                        )
                    ],
                    repair_instruction=RepairInstruction(
                        repair_scope="scene",
                        failure_type="continuity",
                        must_fix=["命名角色「不存在的人」未在当前 chapter 的 subworld 准入名单中。"],
                        must_preserve=["第一章", "开场"],
                        design_patch={"continuity_focus": ["sub_world_unknown_named_entity"]},
                        evidence_refs=["chapter=1", "entity=不存在的人"],
                    ),
                )

        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("hard-final-gate")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="blackbox",
                )
            )
            apply_calls = {"count": 0}
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "hard gate",
                    "setting_summary": "无",
                    "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }
                orchestrator.writer.write_chapter = lambda context: WriterOutput(
                    chapter_number=context.chapter_number,
                    title=f"第{context.chapter_number}章",
                    body="正文" * 900,
                    char_count=1800,
                    end_of_chapter_summary="ok",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )
                orchestrator.review_hub = HardFailReviewHub()
                orchestrator._apply_canon_candidate = lambda **kwargs: apply_calls.__setitem__("count", apply_calls["count"] + 1) or None

                result = orchestrator.run("p", "g", 1)

                session = get_session_factory(get_engine(db_path))()
                try:
                    attempts = session.execute(
                        select(ChapterRewriteAttempt).order_by(ChapterRewriteAttempt.attempt_no)
                    ).scalars().all()
                    latest_draft = session.execute(
                        select(ChapterDraft).order_by(ChapterDraft.version.desc()).limit(1)
                    ).scalar_one()
                    review = session.execute(
                        select(ChapterReview)
                        .where(ChapterReview.draft_id == latest_draft.id)
                        .order_by(ChapterReview.created_at.desc())
                        .limit(1)
                    ).scalar_one()
                    plan = session.execute(select(ChapterPlan)).scalar_one()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(len(attempts), 4)
            self.assertEqual(
                [item.repair_scope for item in attempts],
                ["chapter_plan", "chapter_plan", "band_plan", "band_plan"],
            )
            review_meta = json.loads(review.review_meta_json)
            self.assertEqual((review_meta.get("final_gate_decision") or {}).get("decision"), "manual_review_required")
            self.assertEqual((review_meta.get("final_gate_decision") or {}).get("canon_risk"), "high")
            self.assertEqual(plan.status, "needs_review")
            self.assertEqual(plan.repair_attempt_count, 4)
            self.assertEqual(plan.canon_risk_level, "high")
            self.assertEqual(apply_calls["count"], 0)

    def test_blackbox_draft_repair_uses_transient_overlay_without_persisting_plan(self) -> None:
        class DraftThenPassReviewHub:
            def __init__(self) -> None:
                self.calls = 0

            def review(self, **kwargs) -> ReviewVerdict:  # noqa: ANN003
                self.calls += 1
                if self.calls == 1:
                    return ReviewVerdict(
                        verdict="fail",
                        issues=[
                            ContinuityIssue(
                                rule_name="weak_hook",
                                severity="error",
                                description="章末钩子偏弱",
                                reviewer="webnovel_experience",
                                issue_type="hook_failure",
                                target_scope="scene",
                                evidence_refs=["tail:正文"],
                            )
                        ],
                        repair_instruction=RepairInstruction(
                            repair_scope="scene",
                            failure_type="hook_failure",
                            must_fix=["章末钩子偏弱"],
                            must_preserve=["第一章", "开场"],
                            design_patch={
                                "hook_type": "hard_cliffhanger",
                                "question_hook": "新的悬念问题",
                                "title": "不应持久化的新标题",
                                "chapter_goals": ["不应持久化的新目标"],
                            },
                            evidence_refs=["tail:正文"],
                        ),
                    )
                return ReviewVerdict(verdict="pass", issues=[])

        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("draft-overlay")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="blackbox",
                )
            )
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "draft overlay",
                    "setting_summary": "无",
                    "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }
                orchestrator.writer.write_chapter = lambda context: WriterOutput(
                    chapter_number=context.chapter_number,
                    title=f"第{context.chapter_number}章",
                    body="正文" * 900,
                    char_count=1800,
                    end_of_chapter_summary="ok",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )
                orchestrator.review_hub = DraftThenPassReviewHub()

                result = orchestrator.run("p", "g", 1)

                session = get_session_factory(get_engine(db_path))()
                try:
                    attempts = session.execute(
                        select(ChapterRewriteAttempt).order_by(ChapterRewriteAttempt.attempt_no)
                    ).scalars().all()
                    plan = session.execute(select(ChapterPlan)).scalar_one()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].repair_scope, "draft")
            self.assertEqual(plan.status, "accepted")
            self.assertEqual(plan.title, "第一章")
            self.assertEqual(plan.goals_json, json.dumps(["推进主线"], ensure_ascii=False))
            persisted_experience_plan = json.loads(plan.experience_plan_json)
            self.assertNotEqual(persisted_experience_plan.get("question_hook"), "新的悬念问题")
            result_chapter_plan = json.loads(attempts[0].result_chapter_plan_json)
            self.assertTrue(result_chapter_plan["transient_overlay"])
            self.assertEqual(result_chapter_plan["title"], "第一章")
            self.assertEqual(result_chapter_plan["experience_plan"]["hook_type"], "hard_cliffhanger")
            self.assertEqual(result_chapter_plan["experience_plan"]["question_hook"], "新的悬念问题")

    def test_band_plan_repair_rolls_back_when_lightweight_provisional_fails(self) -> None:
        class AlwaysFailReviewHub:
            def __init__(self) -> None:
                self.calls = 0

            def review(self, **kwargs) -> ReviewVerdict:  # noqa: ANN003
                self.calls += 1
                return ReviewVerdict(
                    verdict="fail",
                    issues=[
                        ContinuityIssue(
                            rule_name="progress_stall",
                            severity="error",
                            description="推进停滞",
                            reviewer="webnovel_experience",
                            issue_type="stall",
                            target_scope="band",
                            evidence_refs=["progress_markers=['推进主线']"],
                        )
                    ],
                    repair_instruction=RepairInstruction(
                        repair_scope="scene" if self.calls == 1 else "band" if self.calls == 2 else "arc",
                        failure_type="stall",
                        must_fix=["推进停滞"],
                        must_preserve=["第一章", "开场"],
                        design_patch={
                            "stall_guard_max_gap": 1,
                            "curiosity_beats": [
                                {
                                    "chapter_hint": 1,
                                    "question_open": "新的危险是什么",
                                    "question_resolve": "确认局部真相",
                                    "escalated_question": "真正的幕后压力是什么",
                                }
                            ],
                        },
                        evidence_refs=["progress_markers=['推进主线']"],
                    ),
                )

        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("band-plan-rollback")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="blackbox",
                )
            )
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "band rollback",
                    "setting_summary": "无",
                    "chapters": [
                        {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]},
                        {"chapter_number": 2, "title": "第二章", "one_line": "升级", "goals": ["放大风险"]},
                    ],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }
                orchestrator.writer.write_chapter = lambda context: WriterOutput(
                    chapter_number=context.chapter_number,
                    title=f"第{context.chapter_number}章",
                    body="正文" * 900,
                    char_count=1800,
                    end_of_chapter_summary="ok",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )
                orchestrator.review_hub = AlwaysFailReviewHub()
                orchestrator._run_provisional_band_preview = lambda **kwargs: SimpleNamespace(
                    aggregate_verdict="fail"
                )

                result = orchestrator.run("p", "g", 2)

                session = get_session_factory(get_engine(db_path))()
                try:
                    band_plan = session.execute(
                        select(BandExperiencePlan).order_by(BandExperiencePlan.created_at.desc()).limit(1)
                    ).scalar_one()
                    chapter_plans = session.execute(
                        select(ChapterPlan).order_by(ChapterPlan.chapter_number.asc())
                    ).scalars().all()
                    attempts = session.execute(
                        select(ChapterRewriteAttempt).order_by(ChapterRewriteAttempt.attempt_no.asc())
                    ).scalars().all()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(
                [item.repair_scope for item in attempts],
                ["draft", "chapter_plan", "chapter_plan", "band_plan", "band_plan"],
            )
            self.assertNotIn("新的危险是什么", band_plan.schedule_json)
            self.assertTrue(all("新的危险是什么" not in (plan.experience_plan_json or "") for plan in chapter_plans))
            self.assertEqual(attempts[-1].failure_reason, "lightweight-provisional:fail")
            self.assertTrue(json.loads(attempts[-1].result_band_plan_json).get("transient_overlay"))

    def test_orchestrator_can_accept_review_and_continue_project(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("resume")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="checkpoint",
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "继续执行",
                        "setting_summary": "无",
                        "chapters": [
                            {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["第1章正文"]},
                            {"chapter_number": 2, "title": "第二章", "one_line": "延续", "goals": ["第2章正文"]},
                        ],
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = f"第{context.chapter_number}章正文" * 520
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
            db_path = postgres_test_url("phase3")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
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
            db_path = postgres_test_url("replan-strategy")
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
            db_path = postgres_test_url("pacing-thresholds")
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
            db_path = postgres_test_url("phase3-api")
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
            db_path = postgres_test_url("projects-with-chapters")
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

    def test_api_projects_only_include_recent_chapter_preview(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("projects-with-chapter-preview")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                arc = updater.create_arc_plan(project.id, "剧情")
                for number in range(1, 76):
                    plan = updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=number,
                        title=f"第{number}章",
                        one_line="推进",
                        goals=["推进"],
                    )
                    session.add(
                        ChapterDraft(
                            chapter_plan_id=plan.id,
                            version=1,
                            body_text=f"正文{number}",
                            summary=f"摘要{number}",
                            char_count=number,
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

        self.assertEqual(payload[0].chapter_count, 75)
        self.assertEqual([row["chapter_number"] for row in payload[0].chapters], [73, 74, 75])

    def test_api_list_chapter_page_returns_page_metadata(self) -> None:
        from forwin import api_project_ops

        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("chapter-page")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)
            session = session_factory()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="分页书", premise="前提", genre="玄幻")
                arc = updater.create_arc_plan(project.id, "剧情")
                for number in range(1, 76):
                    plan = updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=number,
                        title=f"第{number}章",
                        one_line="推进",
                        goals=["推进"],
                    )
                    session.add(
                        ChapterDraft(
                            chapter_plan_id=plan.id,
                            version=1,
                            body_text=f"正文{number}",
                            summary=f"摘要{number}",
                            char_count=number,
                        )
                    )
                session.commit()

                page = api_project_ops.list_chapter_page(
                    project.id,
                    offset=60,
                    limit=20,
                    get_session=session_factory,
                )
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(page.total, 75)
        self.assertEqual(page.offset, 60)
        self.assertEqual(page.limit, 20)
        self.assertFalse(page.has_more)
        self.assertEqual([item.chapter_number for item in page.chapters], list(range(61, 76)))

    def test_api_projects_include_generation_and_upload_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("projects-with-counts")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)
            session = session_factory()
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
                updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=2,
                    title="第二章",
                    one_line="承压",
                    goals=["加压"],
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
                    PublisherUploadJob(
                        project_id=project.id,
                        platform_id="qidian",
                        status="succeeded",
                        book_name="测试书",
                        chapter_title="第一章",
                        body_text="正文一",
                    )
                )
                session.add(
                    PublisherUploadJob(
                        project_id=project.id,
                        platform_id="qidian",
                        status="pending",
                        book_name="测试书",
                        chapter_title="第二章",
                        body_text="正文二",
                    )
                )
                session.commit()

                old_engine = api_module._engine
                old_factory = api_module._SessionFactory
                try:
                    api_module._engine = engine
                    api_module._SessionFactory = session_factory
                    summary = api_module.list_projects()[0]
                    detail = api_module.get_project(project.id)
                finally:
                    api_module._engine = old_engine
                    api_module._SessionFactory = old_factory
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(summary.chapter_count, 2)
        self.assertEqual(summary.generated_chapter_count, 1)
        self.assertEqual(summary.upload_task_count, 2)
        self.assertEqual(summary.uploaded_chapter_count, 1)
        self.assertEqual(detail.upload_task_count, 2)
        self.assertEqual(detail.uploaded_chapter_count, 1)

    def test_create_project_api_creates_book_without_generation_task(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            old_tasks = api_module._tasks
            temp_engine = None
            try:
                api_module._config = Config(
                    database_url=postgres_test_url("books"),
                    runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                    minimax_api_key="",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                temp_engine = get_engine(api_module._config.database_url)
                init_db(temp_engine)
                api_module._engine = temp_engine
                api_module._SessionFactory = get_session_factory(temp_engine)
                api_module._tasks = {}

                created_payload = api_module.create_project(
                    api_module.ProjectCreateRequest(
                        title="测试书",
                        premise="一个从灰城底层醒来的少年",
                        genre="玄幻",
                        setting_summary="高墙外有雾海",
                        target_total_chapters=24,
                    )
                ).model_dump(mode="json")
                projects_payload = [
                    item.model_dump(mode="json")
                    for item in api_module.list_projects()
                ]

                self.assertEqual(created_payload["title"], "测试书")
                self.assertEqual(created_payload["creation_status"], "creating")
                self.assertIn("Genesis 工作台", created_payload["message"])
                self.assertTrue(created_payload["project_id"])
                self.assertEqual(len(projects_payload), 1)
                self.assertEqual(projects_payload[0]["title"], "测试书")
                self.assertEqual(projects_payload[0]["creation_status"], "creating")
                self.assertEqual(created_payload["target_total_chapters"], 24)
                self.assertEqual(projects_payload[0]["target_total_chapters"], 24)
                self.assertEqual(projects_payload[0]["chapter_count"], 0)
                self.assertEqual(projects_payload[0]["uploaded_chapter_count"], 0)
                self.assertEqual(api_module._tasks, {})
            finally:
                if temp_engine is not None:
                    temp_engine.dispose()
                api_module._config = old_config
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory
                api_module._tasks = old_tasks

    def test_create_project_api_persists_platform_book_creation_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            temp_engine = None
            try:
                api_module._config = Config(
                    database_url=postgres_test_url("book-publish-defaults"),
                    runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                    minimax_api_key="",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                temp_engine = get_engine(api_module._config.database_url)
                init_db(temp_engine)
                api_module._engine = temp_engine
                api_module._SessionFactory = get_session_factory(temp_engine)

                created = api_module.create_project(
                    api_module.ProjectCreateRequest(
                        title="测试书",
                        premise="一个从灰城底层醒来的少年",
                        genre="玄幻",
                        setting_summary="高墙外有雾海",
                        target_total_chapters=18,
                        publish_platform="qidian",
                        publish_book_name="灰城见习医师",
                        publish_upload_url="https://writer.example/upload",
                        platform_has_existing_book=False,
                    )
                )
                detail = api_module.get_project(created.project_id)

                self.assertEqual(detail.automation.publish.platform, "qidian")
                self.assertEqual(detail.automation.publish.book_name, "灰城见习医师")
                self.assertEqual(detail.automation.publish.upload_url, "https://writer.example/upload")
                self.assertTrue(detail.automation.publish.create_if_missing)
                self.assertEqual(detail.target_total_chapters, 18)
            finally:
                if temp_engine is not None:
                    temp_engine.dispose()
                api_module._config = old_config
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory

    def test_project_payloads_expose_at_least_planned_chapter_count_as_total(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            temp_engine = None
            try:
                api_module._config = Config(
                    database_url=postgres_test_url("book-total-consistency"),
                    runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                    minimax_api_key="",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                temp_engine = get_engine(api_module._config.database_url)
                init_db(temp_engine)
                api_module._engine = temp_engine
                api_module._SessionFactory = get_session_factory(temp_engine)

                with api_module._SessionFactory() as session:
                    project = Project(
                        title="测试书",
                        premise="前提",
                        genre="玄幻",
                        setting_summary="设定",
                        target_total_chapters=3,
                    )
                    session.add(project)
                    session.flush()
                    arc = ArcPlanVersion(
                        project_id=project.id,
                        version=1,
                        arc_synopsis="弧线",
                        status="active",
                    )
                    session.add(arc)
                    session.flush()
                    session.add_all(
                        [
                            ChapterPlan(
                                project_id=project.id,
                                arc_plan_id=arc.id,
                                chapter_number=chapter_number,
                                title=f"第{chapter_number}章",
                                one_line="推进",
                                goals_json='["目标"]',
                                status="planned",
                            )
                            for chapter_number in range(1, 11)
                        ]
                    )
                    session.commit()
                    project_id = project.id

                summary = api_module.list_projects()[0]
                detail = api_module.get_project(project_id)

                self.assertEqual(summary.chapter_count, 10)
                self.assertEqual(summary.target_total_chapters, 10)
                self.assertEqual(detail.chapter_count, 10)
                self.assertEqual(detail.target_total_chapters, 10)
            finally:
                if temp_engine is not None:
                    temp_engine.dispose()
                api_module._config = old_config
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory

    def test_project_automation_update_roundtrip_exposes_summary_and_detail(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            temp_engine = None
            try:
                api_module._config = Config(
                    database_url=postgres_test_url("book-automation"),
                    runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                    minimax_api_key="saved-key",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                temp_engine = get_engine(api_module._config.database_url)
                init_db(temp_engine)
                api_module._engine = temp_engine
                api_module._SessionFactory = get_session_factory(temp_engine)

                created = api_module.create_project(
                    api_module.ProjectCreateRequest(
                        title="自动化测试书",
                        premise="主角被迫卷入旧城阴谋",
                        genre="悬疑",
                        setting_summary="旧城与雾海相连",
                    )
                )
                updated = api_module.update_project_automation(
                    created.project_id,
                    api_module.ProjectAutomationUpdateRequest.model_validate(
                        {
                            "enabled": True,
                            "daily_start_time": "25:77",
                            "daily_chapter_quota": 99,
                            "auto_publish": True,
                            "publish": {
                                "platform": "qidian",
                                "book_name": "自动化测试书",
                                "upload_url": "https://writer.example/upload",
                                "create_if_missing": True,
                                "book_meta": {
                                    "audience": "男频",
                                    "primary_category": "都市异能",
                                    "protagonist_names": ["韩砚", "顾临"],
                                    "intro": "一场雾夜事故后，主角发现整座旧城都在撒谎。",
                                },
                            },
                        }
                    ),
                )
                summary = api_module.list_projects()[0]
                detail = api_module.get_project(created.project_id)

                self.assertEqual(updated.project_id, created.project_id)
                self.assertEqual(updated.message, "书本自动化设置已保存。")
                self.assertEqual(updated.automation.daily_start_time, "23:59")
                self.assertEqual(updated.automation.daily_chapter_quota, 20)
                self.assertTrue(updated.automation.auto_publish)
                self.assertEqual(updated.automation.publish.platform, "qidian")
                self.assertEqual(summary.automation.daily_start_time, "23:59")
                self.assertEqual(summary.automation.daily_chapter_quota, 20)
                self.assertTrue(summary.automation.enabled)
                self.assertEqual(detail.automation.publish.book_name, "自动化测试书")
                self.assertEqual(detail.automation.publish.book_meta.protagonist_names, ["韩砚", "顾临"])
            finally:
                if temp_engine is not None:
                    temp_engine.dispose()
                api_module._config = old_config
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory

    def test_automation_scheduler_batches_project_metric_queries(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("automation-scheduler")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)
            old_config = api_module._config
            old_factory = api_module._SessionFactory
            initial_calls: list[tuple[str, int]] = []
            continue_calls: list[tuple[str, int, int]] = []

            try:
                with session_factory() as session:
                    ready_payload = json.dumps(
                        {
                            "enabled": True,
                            "daily_start_time": "09:00",
                            "daily_chapter_quota": 2,
                        },
                        ensure_ascii=False,
                    )
                    project_initial = Project(
                        id=new_id(),
                        title="自动调度-首批",
                        premise="前提",
                        genre="玄幻",
                        setting_summary="设定",
                        automation_json=ready_payload,
                    )
                    project_continue = Project(
                        id=new_id(),
                        title="自动调度-续跑",
                        premise="前提",
                        genre="玄幻",
                        setting_summary="设定",
                        automation_json=ready_payload,
                    )
                    project_waiting = Project(
                        id=new_id(),
                        title="自动调度-待审",
                        premise="前提",
                        genre="玄幻",
                        setting_summary="设定",
                        automation_json=ready_payload,
                    )
                    session.add_all([project_initial, project_continue, project_waiting])
                    session.flush()

                    for project in (project_continue, project_waiting):
                        arc = ArcPlanVersion(
                            id=new_id(),
                            project_id=project.id,
                            version=1,
                            arc_synopsis="弧线",
                            status="active",
                        )
                        session.add(arc)
                        session.flush()
                        session.add(
                            ChapterPlan(
                                id=new_id(),
                                project_id=project.id,
                                arc_plan_id=arc.id,
                                chapter_number=1,
                                title="第一章",
                                one_line="推进",
                                goals_json='["目标"]',
                                status="planned" if project.id == project_continue.id else "needs_review",
                            )
                        )
                    session.commit()

                api_module._config = Config(database_url=db_path)
                api_module._SessionFactory = session_factory
                fixed_now = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)

                with capture_select_statements(engine) as select_statements:
                    with patch.object(api_module, "_utcnow", return_value=fixed_now), patch.object(
                        api_module,
                        "_saved_runtime_config_or_503",
                        return_value=Config(database_url=db_path),
                    ), patch.object(
                        api_module,
                        "_create_generation_task",
                        side_effect=lambda **kwargs: initial_calls.append(
                            (str(kwargs.get("title") or ""), int(kwargs.get("num_chapters") or 0))
                        ) or "task-initial",
                    ), patch.object(
                        api_module,
                        "_create_continue_generation_task",
                        side_effect=lambda **kwargs: continue_calls.append(
                            (
                                str(kwargs.get("title") or ""),
                                int(kwargs.get("requested_chapters") or 0),
                                int(kwargs.get("max_chapters") or 0),
                            )
                        ) or "task-continue",
                    ):
                        api_module._run_automation_scheduler_pass()

                with session_factory() as session:
                    projects = {
                        project.title: json.loads(project.automation_json)
                        for project in session.execute(select(Project)).scalars().all()
                    }

                self.assertEqual(count_matching_statements(select_statements, " from projects"), 1)
                self.assertEqual(
                    count_matching_statements(select_statements, " from chapter_plans"),
                    2,
                )
                self.assertEqual(
                    count_matching_statements(select_statements, " from generation_tasks"),
                    1,
                )
                self.assertEqual(initial_calls, [("自动调度-首批", 2)])
                self.assertEqual(continue_calls, [("自动调度-续跑", 1, 2)])
                self.assertEqual(
                    projects["自动调度-首批"]["last_scheduler_action"],
                    "started_initial_generation",
                )
                self.assertEqual(
                    projects["自动调度-续跑"]["last_scheduler_action"],
                    "started_continue_generation",
                )
                self.assertEqual(
                    projects["自动调度-待审"]["last_scheduler_action"],
                    "waiting_review",
                )
            finally:
                api_module._config = old_config
                api_module._SessionFactory = old_factory
                engine.dispose()

    def test_auto_publish_enqueue_batches_plan_draft_and_upload_queries(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("auto-publish")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)
            manager = PublisherManager(session_factory, extension_api_key="secret")
            old_session_factory = api_module._SessionFactory
            old_manager = api_module._publisher_manager

            try:
                with session_factory() as session:
                    project = Project(
                        id=new_id(),
                        title="自动发布测试书",
                        premise="前提",
                        genre="玄幻",
                        setting_summary="设定",
                        automation_json=json.dumps(
                            {
                                "auto_publish": True,
                                "publish": {
                                    "platform": "fanqie",
                                    "book_name": "自动发布测试书",
                                    "create_if_missing": True,
                                },
                            },
                            ensure_ascii=False,
                        ),
                    )
                    session.add(project)
                    session.flush()
                    arc = ArcPlanVersion(
                        id=new_id(),
                        project_id=project.id,
                        version=1,
                        arc_synopsis="弧线",
                        status="active",
                    )
                    session.add(arc)
                    session.flush()
                    plans: list[ChapterPlan] = []
                    for chapter_number in range(1, 4):
                        plan = ChapterPlan(
                            id=new_id(),
                            project_id=project.id,
                            arc_plan_id=arc.id,
                            chapter_number=chapter_number,
                            title=f"第{chapter_number}章",
                            one_line="推进",
                            goals_json='["目标"]',
                            status="accepted",
                        )
                        plans.append(plan)
                    session.add_all(plans)
                    session.flush()
                    session.add_all(
                        [
                            ChapterDraft(
                                id=new_id(),
                                chapter_plan_id=plan.id,
                                version=1,
                                body_text=f"{plan.title} 正文",
                                summary="摘要",
                                char_count=1000,
                            )
                            for plan in plans
                        ]
                    )
                    session.commit()
                    project_id = project.id

                api_module._SessionFactory = session_factory
                api_module._publisher_manager = manager
                with capture_select_statements(engine) as select_statements:
                    api_module._maybe_enqueue_auto_publish_jobs(
                        SimpleNamespace(
                            project_id=project_id,
                            completed_chapters=[1, 2, 3],
                        )
                    )

                with session_factory() as session:
                    stored_jobs = session.execute(
                        select(PublisherUploadJob)
                        .order_by(PublisherUploadJob.chapter_title.asc())
                    ).scalars().all()

                self.assertEqual(len(stored_jobs), 3)
                self.assertEqual([job.chapter_title for job in stored_jobs], ["第1章", "第2章", "第3章"])
                self.assertEqual(count_matching_statements(select_statements, " from projects"), 2)
                self.assertEqual(
                    count_matching_statements(select_statements, " from chapter_plans"),
                    1,
                )
                self.assertEqual(
                    count_matching_statements(select_statements, " from chapter_drafts"),
                    1,
                )
                self.assertEqual(
                    count_matching_statements(select_statements, " from publisher_upload_jobs"),
                    1,
                )
            finally:
                api_module._SessionFactory = old_session_factory
                api_module._publisher_manager = old_manager
                engine.dispose()

    def test_qdrant_memory_index_persists_and_searches_across_instances(self) -> None:
        with TemporaryDirectory() as tmp:
            client = FakeQdrantClient()
            index = QdrantChapterMemoryIndex(
                url="http://qdrant.test:6333",
                collection_name="chapter_memories",
                client=client,
                qdrant_models=FakeQdrantModels,
            )
            index.upsert_chapter(
                project_id="p1",
                chapter_number=1,
                title="幽灵列车",
                summary="失踪三年的末班车忽然回归",
                body="韩砚在雨夜看到那辆本不该出现的末班车。",
            )
            index.upsert_chapter(
                project_id="p1",
                chapter_number=2,
                title="废弃仓库",
                summary="众人转去调查旧仓库线索",
                body="他们在仓库里翻找旧档案。",
            )

            reloaded = QdrantChapterMemoryIndex(
                url="http://qdrant.test:6333",
                collection_name="chapter_memories",
                client=client,
                qdrant_models=FakeQdrantModels,
            )
            hits = reloaded.search(project_id="p1", query="末班车", limit=2)

        self.assertEqual(len(hits), 2)
        self.assertEqual([hit.chapter_number for hit in hits], [1, 2])
        self.assertEqual(hits[0].chapter_number, 1)
        self.assertIn("末班车", hits[0].summary)

    def test_runtime_settings_store_caches_get_results_until_save(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime-settings.json"
            store = RuntimeSettingsStore(
                str(path),
                default_api_key="sk-default",
                default_base_url="https://example.default/v1",
                default_model="default-model",
            )

            first = store.get()
            path.write_text(
                json.dumps(
                    {
                        "api_key": "sk-external",
                        "base_url": "https://example.external/v1",
                        "model": "external-model",
                        "operation_mode": "checkpoint",
                        "freeze_failed_candidates": False,
                        "min_chapter_chars": 2800,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            second = store.get()
            saved = store.save(model="saved-model")
            third = store.get()

        self.assertEqual(first["api_key"], "sk-default")
        self.assertEqual(first["min_chapter_chars"], 2500)
        self.assertEqual(second["api_key"], "sk-default")
        self.assertEqual(saved["model"], "saved-model")
        self.assertEqual(saved["min_chapter_chars"], 2500)
        self.assertEqual(third["model"], "saved-model")

    def test_thread_sampling_balances_stale_and_hot_threads(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("thread-sampling")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            sampled_names: list[str] = []
            updater = None
            project = None
            stale_primary = None
            hot_followup = None
            stale_secondary = None
            sampled = None
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
                sampled_names = [thread.name for thread in sampled.threads]
                sampled = None
            finally:
                updater = None
                project = None
                stale_primary = None
                hot_followup = None
                stale_secondary = None
                sampled = None
                session.close()
                close_all_sessions()
                engine.dispose()
                gc.collect()

        self.assertIn("主线悬案", sampled_names)
        self.assertIn("新线索", sampled_names)
        self.assertLessEqual(len(sampled_names), 3)

    def test_arc_envelope_manager_creates_v24_records(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("arc-envelope")
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
                    provisional_preview_enabled=True,
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
                scenario_rehearsal_count = session.execute(
                    select(func.count(ScenarioRehearsalRunRow.id))
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
        self.assertEqual(scenario_rehearsal_count, 1)

    def test_arc_envelope_backfill_batches_existing_resolution_lookup(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("arc-envelope-backfill")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()

            try:
                updater = StateUpdater(session)
                arc_ids: list[str] = []
                project_ids: list[str] = []
                for index in range(3):
                    project = updater.create_project(
                        title=f"测试书{index + 1}",
                        premise="前提",
                        genre="玄幻",
                    )
                    arc = updater.create_arc_plan(project.id, f"主线弧{index + 1}")
                    arc_ids.append(arc.id)
                    project_ids.append(project.id)
                session.flush()
                session.add_all(
                    [
                        ArcEnvelope(
                            id=new_id(),
                            project_id=project_id,
                            arc_id=arc_id,
                            base_target_size=12,
                            base_soft_min=10,
                            base_soft_max=14,
                            resolved_target_size=12,
                            resolved_soft_min=10,
                            resolved_soft_max=14,
                            detailed_band_size=4,
                            frozen_zone_size=2,
                            current_projected_size=12,
                            current_confidence=0.8,
                            source_policy_tier="short",
                        )
                        for project_id, arc_id in zip(project_ids, arc_ids, strict=False)
                    ]
                )
                session.commit()

                manager = ArcEnvelopeManager(director=None)
                with capture_select_statements(engine) as select_statements:
                    created = manager.backfill_missing_resolutions(session=session)
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(created, 0)
        self.assertEqual(
            count_matching_statements(
                select_statements,
                " from arc_plan_versions join arc_envelopes",
            ),
            1,
        )

    def test_provisional_preview_falls_back_per_chapter_without_disabling_later_previews(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("provisional-degrade")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    artifact_root=str(Path(tmp) / "artifacts"),
                    minimax_api_key="fake-key",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    provisional_preview_enabled=True,
                )
            )
            calls = {"count": 0}
            try:
                engine = get_engine(db_path)
                init_db(engine)
                session = get_session_factory(engine)()
                try:
                    updater = StateUpdater(session)
                    project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                    arc = updater.create_arc_plan(project.id, "主线弧")
                    for chapter_number in range(1, 6):
                        updater.create_chapter_plan(
                            project_id=project.id,
                            arc_plan_id=arc.id,
                            chapter_number=chapter_number,
                            title=f"第{chapter_number}章",
                            one_line=f"推进第{chapter_number}章冲突",
                            goals=["推进", "升级"],
                        )

                    def preview_writer(context, **_kwargs):
                        calls["count"] += 1
                        if calls["count"] == 1:
                            raise RuntimeError("ChapterWriter JSON generation failed after retries: The read operation timed out")
                        return WriterOutput(
                            project_id=project.id,
                            chapter_number=context.chapter_number,
                            title=f"预演第{context.chapter_number}章",
                            body="夜雾压城，顾北沿着堤岸追查海图上新增的坐标。"*8,
                            char_count=len("夜雾压城，顾北沿着堤岸追查海图上新增的坐标。"*8),
                            end_of_chapter_summary=f"第{context.chapter_number}章预演摘要",
                        )

                    orchestrator.provisional_writer.write_preview_chapter = preview_writer
                    orchestrator.arc_envelope_manager.director = None

                    envelope = orchestrator.arc_envelope_manager.ensure_active_arc_resolution(
                        session=session,
                        project_id=project.id,
                        activation_chapter=1,
                    )
                    session.commit()

                    preview = session.execute(
                        select(ProvisionalBandExecution)
                        .where(ProvisionalBandExecution.project_id == project.id)
                        .order_by(ProvisionalBandExecution.created_at.desc())
                        .limit(1)
                    ).scalar_one()
                    ledgers = session.execute(
                        select(ProvisionalChapterLedger)
                        .where(ProvisionalChapterLedger.project_id == project.id)
                        .order_by(ProvisionalChapterLedger.chapter_number.asc())
                    ).scalars().all()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

        self.assertIsNotNone(envelope)
        self.assertEqual(calls["count"], len(ledgers))
        self.assertEqual(preview.aggregate_verdict, "warn")
        self.assertEqual(preview.failure_count, 0)
        self.assertEqual(len(ledgers), 5)
        self.assertEqual(ledgers[0].verdict, "warn")
        self.assertIn("timed out", ledgers[0].error_text.lower())
        self.assertTrue(all(not item.error_text for item in ledgers[1:]))
        self.assertTrue(all(item.title.startswith("预演第") for item in ledgers[1:]))
        self.assertTrue(all(item.char_count > 0 for item in ledgers[1:]))
        self.assertGreater(preview.preview_char_count, 0)

    def test_preview_writer_parses_marker_text_without_json(self) -> None:
        class FakePreviewClient:
            def chat(self, messages, **_kwargs):
                return (
                    "<<FORWIN_TITLE>>\n"
                    "雨夜的古符\n"
                    "<<FORWIN_BODY>>\n"
                    + "林明在废土雨夜中握紧古符，循着残光走向断壁。" * 40
                    + "\n<<FORWIN_SUMMARY>>\n"
                    "林明被古符引向新的废墟入口。"
                )

        writer = ChapterWriter(
            FakePreviewClient(),
            writer_mode="single",
            min_chapter_chars=600,
            max_chapter_chars=1200,
            target_chapter_chars=900,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="玄幻",
            setting_summary="废土",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="古符现身",
            chapter_goals=["推进主线"],
        )

        output = writer.write_preview_chapter(context, max_attempts=1, retry_on_timeout=False)

        self.assertEqual(output.title, "雨夜的古符")
        self.assertIn("林明在废土雨夜中握紧古符", output.body)
        self.assertEqual(output.end_of_chapter_summary, "林明被古符引向新的废墟入口。")
        self.assertEqual(output.generation_meta["mode"], "provisional_preview")

    def test_preview_writer_uses_last_marker_block_when_model_echoes_instruction(self) -> None:
        class FakePreviewClient:
            def chat(self, messages, **_kwargs):
                return (
                    "请按以下结构输出：\n"
                    "【标题】\n"
                    "这里写标题\n"
                    "【正文】\n"
                    "这里写正文\n"
                    "【摘要】\n"
                    "这里写摘要\n"
                    "</think>\n\n"
                    "【标题】\n"
                    "午夜戏台\n"
                    "【正文】\n"
                    + "顾北沿着潮湿台阶走向沉没戏台，耳边全是海风和旧戏腔。" * 30
                    + "\n【摘要】\n"
                    "顾北确认戏台将在下一次满潮时现身。"
                )

        writer = ChapterWriter(
            FakePreviewClient(),
            writer_mode="single",
            min_chapter_chars=600,
            max_chapter_chars=1200,
            target_chapter_chars=900,
        )
        context = ChapterContextPack(
            project_id="p1",
            project_title="测试书",
            premise="前提",
            genre="悬疑奇幻",
            setting_summary="海边旧城",
            chapter_number=3,
            chapter_plan_title="第3章",
            chapter_plan_one_line="戏台出现",
            chapter_goals=["推进主线"],
        )

        output = writer.write_preview_chapter(context, max_attempts=1, retry_on_timeout=False)

        self.assertEqual(output.title, "午夜戏台")
        self.assertIn("顾北沿着潮湿台阶走向沉没戏台", output.body)
        self.assertNotIn("这里写正文", output.body)
        self.assertEqual(output.end_of_chapter_summary, "顾北确认戏台将在下一次满潮时现身。")

    def test_provisional_verdict_softens_fail_when_preview_body_is_usable(self) -> None:
        writer_output = WriterOutput(
            project_id="p1",
            chapter_number=1,
            title="第1章",
            body="正文内容" * 120,
            char_count=len("正文内容" * 120),
            end_of_chapter_summary="摘要",
        )
        verdict = ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="empty_body",
                    severity="error",
                    description="过短",
                    entity_names=[],
                )
            ],
        )

        softened = WritingOrchestrator._normalize_provisional_verdict(writer_output, verdict)

        self.assertEqual(softened.verdict, "warn")
        self.assertTrue(any(issue.rule_name == "provisional_softened_fail" for issue in softened.issues))

    def test_provisional_verdict_ignores_preview_char_count_warning(self) -> None:
        writer_output = WriterOutput(
            project_id="p1",
            chapter_number=1,
            title="第1章",
            body="正文内容" * 80,
            char_count=len("正文内容" * 80),
            end_of_chapter_summary="摘要",
        )
        verdict = ReviewVerdict(
            verdict="warn",
            issues=[
                ContinuityIssue(
                    rule_name="char_count_low",
                    severity="warning",
                    description="预演较短",
                    entity_names=[],
                )
            ],
        )

        normalized = WritingOrchestrator._normalize_provisional_verdict(writer_output, verdict)

        self.assertEqual(normalized.verdict, "pass")
        self.assertEqual(normalized.issues, [])

    def test_provisional_preview_uses_preview_checker_thresholds(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("preview-thresholds")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    artifact_root=str(Path(tmp) / "artifacts"),
                    minimax_api_key="fake-key",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                )
            )
            try:
                engine = get_engine(db_path)
                init_db(engine)
                session = get_session_factory(engine)()
                try:
                    updater = StateUpdater(session)
                    project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                    arc = updater.create_arc_plan(project.id, "主线弧")
                    chapter_plan = updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=1,
                        title="第1章",
                        one_line="预演正文",
                        goals=["推进"],
                    )

                    def preview_writer(_context, **_kwargs):
                        body = "海风沿着旧城墙穿过，顾北在潮声里翻开残卷。"
                        body = body * 20  # > provisional min_chars, < formal 1500
                        return WriterOutput(
                            project_id=project.id,
                            chapter_number=1,
                            title="预演第1章",
                            body=body,
                            char_count=len(body),
                            end_of_chapter_summary="顾北确认残卷与潮汐异象有关。",
                        )

                    orchestrator.provisional_writer.write_preview_chapter = preview_writer
                    preview = orchestrator._run_provisional_band_preview(
                        session=session,
                        project_id=project.id,
                        arc_id=arc.id,
                        band_id="band:1:1",
                        chapter_plans=[chapter_plan],
                    )
                    session.commit()
                    ledger = session.execute(
                        select(ProvisionalChapterLedger)
                        .where(ProvisionalChapterLedger.project_id == project.id)
                        .limit(1)
                    ).scalar_one()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

        self.assertIsNotNone(preview)
        self.assertEqual(preview.aggregate_verdict, "pass")
        self.assertEqual(preview.issue_count, 0)
        self.assertEqual(ledger.verdict, "pass")
        self.assertEqual(json.loads(ledger.issues_json), [])

    def test_orchestrator_emits_project_created_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("progress-callback")
            progress_events: list[tuple[str, dict[str, object]]] = []
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    artifact_root=str(Path(tmp) / "artifacts"),
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                ),
                progress_callback=lambda event, payload: progress_events.append((event, payload)),
            )
            try:
                with patch.object(
                    orchestrator.arc_director,
                    "plan_arc",
                    return_value={
                        "arc_synopsis": "测试主线",
                        "setting_summary": "测试世界",
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "threads": [],
                        "chapter_plans": [
                            {
                                "chapter_number": 1,
                                "title": "第一章",
                                "one_line": "开场",
                                "goals": ["推进"],
                            }
                        ],
                    },
                ), patch.object(
                    orchestrator,
                    "_run_project_chapters",
                    side_effect=lambda **kwargs: RunResult(
                        project_id=kwargs["project_id"],
                        requested_chapters=kwargs["requested_chapters"],
                    ),
                ):
                    result = orchestrator.run("前提", "玄幻", 1)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

        self.assertEqual(result.status, "completed")
        project_created = [
            payload
            for event, payload in progress_events
            if event == "project_created"
        ]
        self.assertEqual(len(project_created), 1)
        self.assertEqual(project_created[0]["project_id"], result.project_id)
        self.assertEqual(project_created[0]["requested_chapters"], 1)
        self.assertEqual(project_created[0]["title"], "测试主线")

    def test_api_projects_expose_active_arc_envelope_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("arc-envelope-api")
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
                session.add(
                    ArcEnvelopeAnalysis(
                        project_id=project.id,
                        arc_id=arc.id,
                        based_on_band_id="band:1:6",
                        recommendation="expand",
                        evidence_json='["热点后移", "主冲突仍在升温"]',
                        expansion_signals_json='["主高潮尚未到来"]',
                        compression_signals_json='[]',
                        suggested_target=22,
                        suggested_soft_min=16,
                        suggested_soft_max=28,
                        confidence=0.86,
                    )
                )
                session.add(
                    ProvisionalBandExecution(
                        project_id=project.id,
                        arc_id=arc.id,
                        band_id="band:1:6",
                        chapter_numbers_json="[1, 2, 3]",
                        artifact_path="provisional://band.json",
                        aggregate_verdict="warn",
                        preview_char_count=4200,
                        issue_count=2,
                        failure_count=0,
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
        self.assertEqual(payload[0].active_arc_recommendation, "expand")
        self.assertEqual(payload[0].provisional_band_id, "band:1:6")
        self.assertEqual(detail.active_arc_detailed_band_size, 8)
        self.assertEqual(detail.active_arc_frozen_zone_size, 3)
        self.assertEqual(detail.active_arc_evidence[0], "热点后移")
        self.assertEqual(detail.provisional_aggregate_verdict, "warn")

    def test_api_projects_created_at_uses_pacific_display_time(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("projects-timezone")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                project.created_at = datetime(2026, 4, 2, 19, 0, 0, tzinfo=timezone.utc)
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

        self.assertEqual(payload[0].created_at, "2026-04-02 12:00:00 PDT")

    def test_api_exposes_latest_provisional_band_shadow_ledger(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("provisional-api")
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

    def test_api_exposes_latest_provisional_band_pass_verdict(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("provisional-pass-api")
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
                        band_id="band:1:1",
                        chapter_numbers_json="[1]",
                        artifact_path="provisional://pass-band.json",
                        aggregate_verdict="pass",
                        preview_char_count=900,
                        issue_count=0,
                        failure_count=0,
                    )
                )
                session.add(
                    ProvisionalChapterLedger(
                        project_id=project.id,
                        arc_id=arc.id,
                        band_id="band:1:1",
                        chapter_number=1,
                        title="第一章",
                        summary="预演摘要",
                        verdict="pass",
                        char_count=900,
                        artifact_meta_path="meta://1",
                        draft_blob_path="draft://1",
                        current_time_label="第一日夜",
                        projected_time_label="第二日晨",
                        state_changes_json="[]",
                        events_json="[]",
                        thread_beats_json="[]",
                        time_advance_json="{}",
                        issues_json="[]",
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

        self.assertEqual(payload.aggregate_verdict, "pass")
        self.assertEqual(payload.issue_count, 0)
        self.assertEqual(payload.chapters[0].verdict, "pass")

    def test_phase24_uses_ultra_long_tier_for_epic_projects(self) -> None:
        policy = policy_for_total_chapters(1000)

        self.assertEqual(policy.name, "ultra-long")
        self.assertEqual(policy.min_size, 24)
        self.assertEqual(policy.max_size, 48)

    def test_api_list_chapters_uses_latest_draft_values(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("chapter-list")
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

    def test_latest_rewrite_attempts_by_chapter_returns_one_latest_attempt_per_chapter(self) -> None:
        from forwin.api_project_ops import latest_rewrite_attempts_by_chapter

        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("api-list-chapters-latest-attempt-helper")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                arc = updater.create_arc_plan(project.id, "剧情")
                plan_1 = updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    one_line="开场",
                    goals=["推进"],
                )
                plan_2 = updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=2,
                    title="第二章",
                    one_line="转折",
                    goals=["推进"],
                )
                draft_1 = ChapterDraft(chapter_plan_id=plan_1.id, version=1, body_text="正文一", summary="", char_count=3)
                draft_2 = ChapterDraft(chapter_plan_id=plan_2.id, version=1, body_text="正文二", summary="", char_count=3)
                session.add_all([draft_1, draft_2])
                session.flush()
                review_1 = ChapterReview(draft_id=draft_1.id, verdict="needs_revision")
                review_2 = ChapterReview(draft_id=draft_2.id, verdict="needs_revision")
                session.add_all([review_1, review_2])
                session.flush()
                session.add_all(
                    [
                        ChapterRewriteAttempt(
                            project_id=project.id,
                            chapter_number=1,
                            attempt_no=1,
                            trigger_review_id=review_1.id,
                            source_draft_id=draft_1.id,
                            result_draft_id=draft_1.id,
                            repair_scope="chapter_plan",
                            created_at=datetime(2026, 1, 1, 0, 0, 0),
                        ),
                        ChapterRewriteAttempt(
                            project_id=project.id,
                            chapter_number=1,
                            attempt_no=2,
                            trigger_review_id=review_1.id,
                            source_draft_id=draft_1.id,
                            result_draft_id=draft_1.id,
                            repair_scope="draft",
                            created_at=datetime(2026, 1, 1, 0, 1, 0),
                        ),
                        ChapterRewriteAttempt(
                            project_id=project.id,
                            chapter_number=2,
                            attempt_no=1,
                            trigger_review_id=review_2.id,
                            source_draft_id=draft_2.id,
                            result_draft_id=draft_2.id,
                            repair_scope="band_plan",
                            created_at=datetime(2026, 1, 1, 0, 2, 0),
                        ),
                    ]
                )
                session.commit()

                attempts = latest_rewrite_attempts_by_chapter(session, project.id)
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(sorted(attempts), [1, 2])
        self.assertEqual(attempts[1].attempt_no, 2)
        self.assertEqual(attempts[1].repair_scope, "draft")
        self.assertEqual(attempts[2].repair_scope, "band_plan")

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
            audience_hints=AudienceHintView(
                pacing_hints=["下一章尽快推进失踪信号主线。"],
            ),
            reader_feedback=ReaderFeedbackView(
                comment_count=12,
                dominant_sentiment="期待",
                feedback_summary="读者期待主角尽快追上失踪信号。",
                recent_highlights=[
                    ReaderCommentView(
                        platform_id="fanqie",
                        author_name="海边听潮",
                        body_text="这个伏笔埋得很稳，快让主角去追信号。",
                        remote_created_at="2026-04-02 12:00:00 PDT",
                    )
                ],
            ),
            retrieved_memories=[
                MemorySnippet(
                    chapter_number=2,
                    title="旧站台",
                    summary="主角在旧站台首次发现异常潮声。",
                    excerpt="主角在旧站台首次发现异常潮声，并意识到信号来自海边。",
                    score=0.91,
                )
            ],
            current_arc_envelope=ArcEnvelopeView(
                base_target_size=12,
                base_soft_min=9,
                base_soft_max=15,
                resolved_target_size=12,
                resolved_soft_min=9,
                resolved_soft_max=15,
                detailed_band_size=5,
                frozen_zone_size=2,
                current_projected_size=12,
                current_confidence=0.82,
                source_policy_tier="short",
            ),
            timeline=TimelineSnapshot(
                current_time_label="第二日夜",
                ordinal=2,
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
        self.assertIn("【读者信号提示（仅供参考，自然融入情节）】", prompt[1]["content"])
        self.assertNotIn("【读者反馈】", prompt[1]["content"])
        self.assertIn("【检索到的关键记忆】", prompt[1]["content"])
        self.assertIn("【当前 Arc Envelope】", prompt[1]["content"])
        self.assertIn("【当前时间】", prompt[1]["content"])

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
        self.assertIn("读者信号提示", scene_prompt[1]["content"])
        self.assertNotIn("读者反馈", scene_prompt[1]["content"])
        self.assertIn("检索到的关键记忆", scene_prompt[1]["content"])
        self.assertIn("当前 Arc Envelope", scene_prompt[1]["content"])
        self.assertIn("当前时间", scene_prompt[1]["content"])

    def test_state_repository_active_entities_uses_latest_state_and_alias_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("repo-entities")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                entity = updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="韩砚",
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

    def test_prompt_excludes_reader_feedback_but_keeps_audience_hints(self) -> None:
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
            audience_hints=AudienceHintView(
                pacing_hints=["下一章更快兑现主线推进。"],
                risk_flags=["避免连续两章只堆设定。"],
            ),
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
        self.assertIn("【读者信号提示（仅供参考，自然融入情节）】", prompt[1]["content"])
        self.assertIn("下一章更快兑现主线推进。", prompt[1]["content"])
        self.assertIn("避免连续两章只堆设定。", prompt[1]["content"])
        self.assertNotIn("【读者反馈】", prompt[1]["content"])
        self.assertNotIn("最近 3 条评论，读者对悬念追问较多。", prompt[1]["content"])
        self.assertNotIn("悬念追问", prompt[1]["content"])
        self.assertNotIn("为什么林夜还不反击", prompt[1]["content"])

    def test_phase24_bridge_chapter_does_not_invent_reward_tags(self) -> None:
        from forwin.orchestrator.phase24 import ArcStructureDraftData
        from forwin.protocol.experience import (
            ArcPayoffMap,
            BandDelightSchedule,
            BandRewardItem,
            CuriosityBeat,
            ReaderPromise,
        )

        manager = ArcEnvelopeManager(director=SimpleNamespace())
        plan = manager._derive_chapter_experience_plan(
            chapter_number=2,
            structure=ArcStructureDraftData(
                phase_layout=["setup", "pressure", "payoff"],
                key_beats=["主角被盯上", "确认代价", "发现反扑入口"],
                thread_priorities=[],
                hotspot_candidates=[],
                compression_candidates=[],
                reader_promise=ReaderPromise(world_legibility_target="规则必须可验证"),
                arc_payoff_map=ArcPayoffMap(ambiguity_constraints=["每次动用异象都要付出代价"]),
            ),
            schedule=BandDelightSchedule(
                band_id="band:1:3",
                chapter_start=1,
                chapter_end=3,
                scheduled_rewards=[
                    BandRewardItem(
                        chapter_hint=1,
                        category="power",
                        template_id="power-micro-win",
                        intent="micro_progress_power",
                    ),
                    BandRewardItem(
                        chapter_hint=3,
                        category="mystery",
                        template_id="mystery-locked-clue",
                        intent="mystery_clue_or_reveal",
                    ),
                ],
                immersion_anchor_scene_goal="让读者听见站台风声",
                stall_guard_max_gap=1,
                curiosity_beats=[
                    CuriosityBeat(
                        chapter_hint=2,
                        question_open="这次代价到底落在谁身上",
                        question_resolve="确认异象不是无成本外挂",
                        escalated_question="真正的操盘者为什么主动放出线索",
                    )
                ],
            ),
            chapter_plan=ChapterPlan(
                id=new_id(),
                project_id="p1",
                arc_plan_id="arc-1",
                chapter_number=2,
                title="第二章",
                one_line="桥段承压",
                goals_json=json.dumps(["推进后果", "确认代价"], ensure_ascii=False),
                status="planned",
            ),
        )

        self.assertEqual(plan.planned_reward_tags, [])
        self.assertEqual(plan.selected_template_ids, [])
        self.assertEqual(plan.question_hook, "这次代价到底落在谁身上")
        self.assertTrue(plan.progress_markers)

    def test_single_and_preview_prompts_include_experience_overlay(self) -> None:
        from forwin.protocol.experience import (
            BandDelightSchedule,
            BandRewardItem,
            ChapterExperiencePlan,
            ReaderPromise,
        )

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
            reader_promise=ReaderPromise(
                genre_promise="玄幻升级文",
                pleasure_promise="稳定爽点与悬念回报",
                core_pleasures=["悬念", "翻盘"],
                cliffhanger_aggressiveness="high",
            ),
            band_delight_schedule=BandDelightSchedule(
                band_id="band:1:3",
                chapter_start=1,
                chapter_end=3,
                scheduled_rewards=[
                    BandRewardItem(
                        chapter_hint=2,
                        category="mystery",
                        template_id="mystery-locked-clue",
                        intent="mystery_clue_or_reveal",
                    )
                ],
                immersion_anchor_scene_goal="让读者进入现场",
                stall_guard_max_gap=1,
            ),
            chapter_experience_plan=ChapterExperiencePlan(
                planned_reward_tags=["mystery"],
                selected_template_ids=["mystery-locked-clue"],
                hook_type="cliffhanger_question",
                question_hook="幕后推手到底是谁",
                question_resolution="确认一条真线索",
                immersion_anchors=["雨夜站台的风声"],
                progress_markers=["主角确认线索来源"],
                rule_anchors=["异象必须遵守代价"],
            ),
        )

        single_prompt = build_single_chapter_draft_prompt(context)
        preview_prompt = build_preview_chapter_prompt(context)

        self.assertIn("【读者体验 Overlay】", single_prompt[1]["content"])
        self.assertIn("本章计划奖励：mystery", single_prompt[1]["content"])
        self.assertIn("幕后推手到底是谁", single_prompt[1]["content"])
        self.assertIn("【读者体验 Overlay】", preview_prompt[1]["content"])
        self.assertIn("异象必须遵守代价", preview_prompt[1]["content"])

    def test_lint_signal_collector_does_not_autorun_reviewdog_for_plain_text(self) -> None:
        from forwin.reviewer.lint import LintSignalCollector

        collector = LintSignalCollector(enabled=True)
        invoked_tools: list[str] = []

        with patch("forwin.reviewer.lint.shutil.which", return_value="/usr/bin/fake-tool"):
            with patch.object(
                collector,
                "_run_tool",
                side_effect=lambda tool, _path: invoked_tools.append(tool) or [],
            ):
                collector.collect(
                    WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body="这是一段普通正文，不是 rdjson 诊断流。",
                        char_count=18,
                        end_of_chapter_summary="ok",
                        state_changes=[],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )
                )

        self.assertEqual(invoked_tools, ["vale", "textlint", "languagetool"])

    def test_phase4_generators_can_use_llm_output(self) -> None:
        class FakeIntentLLM:
            def __init__(self) -> None:
                self.kwargs: list[dict[str, object]] = []

            def chat(self, messages, temperature: float, max_tokens: int, response_format=None, **kwargs):
                self.kwargs.append(dict(kwargs))
                return '{"intents":[{"entity_name":"林夜","intent_kind":"pressure","objective":"逼近失踪信号核心","tactic":"先制造信息差再逼问","urgency":5,"notes":"下章立即执行"}]}'

        class FakeWorldLLM:
            def __init__(self) -> None:
                self.kwargs: list[dict[str, object]] = []

            def chat(self, messages, temperature: float, max_tokens: int, response_format=None, **kwargs):
                self.kwargs.append(dict(kwargs))
                return '{"pressure_level":"critical","pressure_summary":"世界开始围堵主角。","notable_shifts":["失踪信号扩散","同盟开始动摇"]}'

        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("phase4-llm")
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

                intent_llm = FakeIntentLLM()
                world_llm = FakeWorldLLM()
                intents = NPCIntentGenerator(llm_client=intent_llm).generate(
                    session=session,
                    project_id=project.id,
                    chapter_number=1,
                )
                world = WorldSimulator(llm_client=world_llm).simulate(
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
        self.assertEqual(intent_llm.kwargs[0]["timeout_seconds"], 8.0)
        self.assertIs(intent_llm.kwargs[0]["retry_on_timeout"], False)
        self.assertEqual(world_llm.kwargs[0]["timeout_seconds"], 8.0)
        self.assertIs(world_llm.kwargs[0]["retry_on_timeout"], False)

    def test_orchestrator_records_phase4_outputs(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("phase4")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
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
            self.assertEqual(
                [
                    (
                        intent.chapter_number,
                        intent.entity_name,
                        intent.intent_kind,
                        intent.tactic,
                        intent.urgency,
                    )
                    for intent in intents
                ],
                [],
            )
            self.assertEqual(
                [
                    (turn.chapter_number, turn.pressure_level, turn.pressure_summary)
                    for turn in turns
                ],
                [
                    (1, "steady", "第1章后，世界压力为 steady。重点变化：主要矛盾仍处于可控推进状态。"),
                    (2, "rising", "第2章后，世界压力为 rising。重点变化：悬置线程：失踪信号。"),
                ],
            )

    def test_repo_summarizes_reader_feedback_by_work_name(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("reader-feedback")
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
        self.assertEqual(feedback.dominant_sentiment, "curious")
        self.assertEqual(feedback.feedback_summary, "最近 2 条评论，读者对悬念追问较多。")
        self.assertEqual(len(feedback.recent_highlights), 2)
        self.assertEqual(
            {item.author_name for item in feedback.recent_highlights},
            {"读者A", "读者B"},
        )

    def test_repo_reader_feedback_prefers_project_scope_and_respects_before_chapter_titles(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("reader-feedback-scope")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project_a = updater.create_project(title="重名书", premise="前提A", genre="玄幻")
                project_b = updater.create_project(title="重名书", premise="前提B", genre="玄幻")
                arc_a = updater.create_arc_plan(project_a.id, "弧线A")
                arc_b = updater.create_arc_plan(project_b.id, "弧线B")
                updater.create_chapter_plan(project_a.id, arc_a.id, 1, "第一章", "一", ["g1"])
                updater.create_chapter_plan(project_a.id, arc_a.id, 2, "第二章", "二", ["g2"])
                updater.create_chapter_plan(project_b.id, arc_b.id, 1, "第一章", "一", ["g1"])
                updater.create_chapter_plan(project_b.id, arc_b.id, 2, "第二章", "二", ["g2"])
                session.add_all(
                    [
                        PublisherRawComment(
                            project_id=project_a.id,
                            platform_id="qidian",
                            remote_comment_id="a-c1",
                            work_name="重名书",
                            chapter_title="第一章",
                            author_name="读者A",
                            body_text="A 项目的第一章反馈",
                        ),
                        PublisherRawComment(
                            project_id=project_a.id,
                            platform_id="qidian",
                            remote_comment_id="a-c2",
                            work_name="重名书",
                            chapter_title="第二章",
                            author_name="读者A2",
                            body_text="A 项目的第二章反馈，不该出现在 before_chapter=2 中",
                        ),
                        PublisherRawComment(
                            project_id=project_b.id,
                            platform_id="fanqie",
                            remote_comment_id="b-c1",
                            work_name="重名书",
                            chapter_title="第一章",
                            author_name="读者B",
                            body_text="B 项目的反馈，不该串到 A",
                        ),
                    ]
                )
                session.commit()

                feedback = StateRepository(session).get_recent_reader_feedback(
                    project_a.id,
                    before_chapter=2,
                )
            finally:
                session.close()
                engine.dispose()

        self.assertIsNotNone(feedback)
        assert feedback is not None
        self.assertEqual(feedback.comment_count, 1)
        self.assertEqual(len(feedback.recent_highlights), 1)
        self.assertIn("A 项目的第一章反馈", feedback.recent_highlights[0].body_text)

    def test_phase4_rule_fallback_uses_reader_feedback(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("phase4-feedback")
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

        self.assertEqual(
            [
                (
                    item.entity_name,
                    item.intent_kind,
                    item.objective,
                    item.tactic,
                    item.urgency,
                    item.notes,
                )
                for item in intents
            ],
            [
                (
                    "林夜",
                    "pressure",
                    "围绕失踪信号采取下一步行动，争取在下一章改变局面。",
                    "主动制造信息差；优先回应读者困惑较多的悬念信息点",
                    5,
                    "重要度10，第3章前生效。",
                )
            ],
        )
        self.assertEqual(
            (
                world.pressure_level,
                world.pressure_summary,
                world.notable_shifts,
            ),
            (
                "rising",
                "第2章后，世界压力为 rising。重点变化：悬置线程：失踪信号；读者困惑点正在放大世界压迫感。",
                ["悬置线程：失踪信号", "读者困惑点正在放大世界压迫感"],
            ),
        )

    def test_blackbox_writer_failure_fails_without_empty_review(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("blackbox-attention")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
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

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.paused_chapters, [])
            self.assertEqual(result.failed_chapters, [1])
            self.assertEqual(plan.status, "failed")
            self.assertEqual(len(result.frozen_artifacts), 1)

    def test_blackbox_timeout_stops_extra_writer_retries(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("blackbox-timeout")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
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

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failed_chapters, [1])
            self.assertEqual(calls["count"], 1)

    def test_provisional_failure_blocks_formal_writing(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("provisional-gate")
            events: list[tuple[str, dict[str, object]]] = []
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="test-key",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="blackbox",
                    provisional_preview_enabled=True,
                ),
                progress_callback=lambda event, payload: events.append((event, dict(payload))),
            )
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "预演失败",
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
                orchestrator.arc_envelope_manager.director = None
                orchestrator.provisional_writer.write_preview_chapter = (
                    lambda _context, **_kwargs: (_ for _ in ()).throw(
                        RuntimeError("529 Unknown Status Code")
                    )
                )
                orchestrator.writer.write_chapter = lambda _context: WriterOutput(
                    chapter_number=1,
                    title="第一章",
                    body="正文" * 800,
                    char_count=len("正文" * 800),
                    end_of_chapter_summary="正式正文摘要",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )

                result = orchestrator.run("p", "g", 1)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    plan = session.execute(select(ChapterPlan)).scalar_one()
                    preview = session.execute(
                        select(ProvisionalBandExecution)
                        .where(ProvisionalBandExecution.project_id == result.project_id)
                        .limit(1)
                    ).scalar_one()
                    ledger = session.execute(
                        select(ProvisionalChapterLedger)
                        .where(ProvisionalChapterLedger.project_id == result.project_id)
                        .limit(1)
                    ).scalar_one()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.failed_chapters, [])
        self.assertEqual(result.paused_chapters, [])
        self.assertEqual(plan.status, "accepted")
        self.assertEqual(preview.aggregate_verdict, "warn")
        self.assertEqual(preview.failure_count, 0)
        self.assertEqual(ledger.verdict, "warn")
        self.assertIn("529", ledger.error_text)
        self.assertNotIn(
            "provisional_failed",
            [payload.get("stage") for event, payload in events if event == "stage_changed"],
        )

    def test_api_project_detail_includes_phase4_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("phase4-api")
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
            db_path = postgres_test_url("phase3-strategy-api")
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

    def test_project_runtime_history_queries_use_windowed_limits(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("runtime-history")
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
                session.add_all(
                    [
                        ProjectReplanEvent(
                            project_id=project.id,
                            trigger_chapter=chapter_number,
                            risk_level="medium",
                            reason=f"replan-{chapter_number}",
                            focus_threads_json='["主线"]',
                            strategy="reband",
                            status="applied",
                            cooldown_until_chapter=chapter_number + 2,
                        )
                        for chapter_number in range(1, 9)
                    ]
                )
                session.add_all(
                    [
                        NPCIntentSnapshot(
                            project_id=project.id,
                            chapter_number=chapter_number,
                            entity_id=entity.id,
                            entity_name="林夜",
                            intent_kind="pursue",
                            objective=f"目标-{chapter_number}",
                            tactic="制造信息差",
                            urgency=chapter_number,
                            notes="下章前生效",
                        )
                        for chapter_number in range(1, 10)
                    ]
                )
                session.commit()

                with capture_select_statements(engine) as select_statements:
                    recent_replans = api_project_payloads_module.load_recent_replan_events_by_project(
                        session,
                        [project.id],
                        limit=5,
                    )
                    recent_intents = api_project_payloads_module.load_recent_npc_intents_by_project(
                        session,
                        [project.id],
                        limit=6,
                    )
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(
            [item.trigger_chapter for item in recent_replans[project.id]],
            [8, 7, 6, 5, 4],
        )
        self.assertEqual(
            [item.chapter_number for item in recent_intents[project.id]],
            [9, 8, 7, 6, 5, 4],
        )
        replan_queries = [
            statement for statement in select_statements if "project_replan_events" in statement
        ]
        npc_queries = [
            statement for statement in select_statements if "npc_intent_snapshots" in statement
        ]
        self.assertEqual(len(replan_queries), 1)
        self.assertEqual(len(npc_queries), 1)
        self.assertIn("row_number() over", replan_queries[0])
        self.assertIn("row_number() over", npc_queries[0])

    def test_apply_events_rejects_partial_event_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("events")
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

    def test_apply_state_changes_rejects_unknown_character_under_subworld_control(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("same-chapter-events")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")

                with self.assertRaisesRegex(ValueError, "unknown or unapproved entity"):
                    updater.apply_state_changes(
                        project_id=project.id,
                        chapter_number=1,
                        changes=[
                            StateChangeCandidate(
                                entity_name="小明",
                                entity_kind="character",
                                field="location",
                                old_value="",
                                new_value="外院",
                                reason="首次登场",
                            )
                        ],
                    )
            finally:
                session.close()
                engine.dispose()

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

    def test_arc_director_json_retry_keeps_token_budget_after_parse_failure(self) -> None:
        class FakeLLMClient:
            def __init__(self) -> None:
                self.max_tokens_seen: list[int] = []

            def chat(self, _messages, temperature: float, max_tokens: int) -> str:
                self.max_tokens_seen.append(max_tokens)
                return '{"phase_layout": {"phase_1":'

        client = FakeLLMClient()
        director = ArcDirector(client, max_tokens=4096)

        payload = director._call_json(
            [{"role": "user", "content": "只输出 JSON"}],
            temperature=0.4,
            max_tokens=1800,
            fallback={"fallback": True},
            stage_key="arc_plan",
        )

        self.assertEqual(payload, {"fallback": True})
        self.assertEqual(client.max_tokens_seen, [1800, 1800, 1800])

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
        class FakeMemoryIndex:
            def search(self, **_kwargs):
                return []

        broker = RetrievalBroker(
            context_budget_chars=420,
            max_threads=4,
            memory_index=FakeMemoryIndex(),
        )
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
            db_path = postgres_test_url("schema")
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
            db_path = postgres_test_url("aliases")
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
                    return '{"title":"第一章","body":"%s。"}' % ("正文" * 900)
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
            memory_index=type("FakeMemoryIndex", (), {"search": lambda self, **_kwargs: []})(),
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
                backend="qdrant",
                root_dir=tmp,
                qdrant_url="http://qdrant.test:6333",
                qdrant_collection="chapter_memories",
                qdrant_client=FakeQdrantClient(),
                qdrant_models=FakeQdrantModels,
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
                    '{"text":"scene one。","micro_summary":"相遇总结","scene_time_point":"清晨","scene_location_id":"荒原","involved_entities":["林夜"]}',
                    '{"text":"scene two。","micro_summary":"逃离总结","scene_time_point":"午后","scene_location_id":"峡谷","involved_entities":["林夜"]}',
                    '{"title":"拼接章","body":"scene one。\\nscene two。","end_of_chapter_summary":"本章完成逃离"}',
                    '{"state_changes":[{"entity_name":"林夜","entity_kind":"character","field":"location","old_value":"荒原","new_value":"峡谷","reason":"逃离成功"}],"new_events":[{"summary":"林夜逃离","significance":"major","involved_entity_names":["林夜"],"roles":["protagonist"]}]}',
                    '{"thread_beats":[{"thread_name":"主线","beat_type":"escalation","description":"危机升级"}],"time_advance":{"new_time_label":"第二天","duration_description":"半日后"}}',
                    '{"lore_candidates":[],"timeline_hints":[],"writer_notes":[]}',
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
        self.assertEqual(output.generation_meta["call_count"], 7)
        self.assertEqual(output.state_changes[0].field, "location")

    def test_scene_writer_falls_back_when_scene_breakdown_json_fails(self) -> None:
        class FakeLLMClient:
            def __init__(self) -> None:
                self.responses = [
                    "scene breakdown unavailable",
                    '{"text":"scene one。","micro_summary":"相遇总结","scene_time_point":"清晨","scene_location_id":"荒原","involved_entities":["林夜"]}',
                    '{"text":"scene two。","micro_summary":"推进总结","scene_time_point":"午后","scene_location_id":"旧站台","involved_entities":["林夜"]}',
                    '{"title":"回退章","body":"scene one。\\nscene two。","end_of_chapter_summary":"本章完成推进"}',
                    '{"state_changes":[],"new_events":[]}',
                    '{"thread_beats":[],"time_advance":null}',
                    '{"lore_candidates":[],"timeline_hints":[],"writer_notes":[]}',
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
                    '{"state_changes":[],"new_events":[]}',
                    '{"thread_beats":[],"time_advance":null}',
                    '{"lore_candidates":[],"timeline_hints":[],"writer_notes":[]}',
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

    def _make_publisher_manager(
        self,
        preferred_client_id: str = "",
        strict_preferred_client: bool = False,
    ) -> tuple[TemporaryDirectory, object, PublisherManager]:
        tmp = TemporaryDirectory()
        db_path = postgres_test_url("publisher")
        engine = get_engine(db_path)
        init_db(engine)
        session_factory = get_session_factory(engine)
        return tmp, engine, PublisherManager(
            session_factory,
            extension_api_key="secret",
            preferred_client_id=preferred_client_id,
            strict_preferred_client=strict_preferred_client,
        )

    def test_publisher_manager_tracks_extension_heartbeat_and_stale_state(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertEqual(
                set(items["qidian"].keys()),
                {
                    "platform_id",
                    "display_name",
                    "login_url",
                    "dashboard_url",
                    "publish_url",
                    "supported_login_methods",
                    "supported_actions",
                    "connected",
                    "extension_online",
                    "last_heartbeat_at",
                    "last_error",
                    "extension_client_id",
                    "preferred_client_state",
                    "latest_client_state",
                    "global_platform_state",
                    "browser_session_state",
                    "fallback_available",
                    "fallback_client_id",
                },
            )
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
                        "cookie_signal": True,
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
            manager.record_extension_heartbeat(
                client_id="client-1",
                extension_version="0.1.1",
                browser_name="Chrome",
                browser_version="124.0",
                backend_base_url="http://192.168.31.10:8899",
                platforms=[
                    {
                        "platform": "qidian",
                        "connected": True,
                        "cookie_signal": True,
                        "login_method": "scan",
                        "last_error": "",
                    },
                ],
            )

            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertTrue(items["qidian"]["connected"])
            self.assertTrue(items["qidian"]["extension_online"])
            self.assertEqual(items["fanqie"]["last_error"], "等待扫码")
            self.assertTrue(items["qidian"]["last_heartbeat_at"].endswith("PDT"))

            with manager.session_factory() as session:
                client = session.get(PublisherExtensionClient, "client-1")
                self.assertEqual(client.extension_version, "0.1.1")
                self.assertEqual(client.browser_version, "124.0")
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
            with manager.session_factory() as session:
                updater = StateUpdater(session)
                updater.create_project(title="测试书", premise="前提", genre="玄幻")
                session.commit()

            job = manager.create_upload_job(
                platform="qidian",
                book_name="测试书",
                chapter_title="第一章",
                body="正文",
                upload_url=None,
                publish=True,
            )
            self.assertEqual(job["status"], "pending")
            self.assertTrue(job["project_id"])

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
            claimed_sync_job = manager.claim_next_comment_sync_job(
                client_id="client-1",
                connected_platforms=["fanqie"],
            )
            self.assertIsNotNone(claimed_sync_job)
            self.assertEqual(claimed_sync_job["status"], "running")

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
            finished_sync_job = manager.update_comment_sync_job_result(
                job_id=sync_job["job_id"],
                client_id="client-1",
                status="succeeded",
                message="评论同步已完成。",
                error="",
                result_payload={"fetched_count": 2},
            )
            self.assertEqual(finished_sync_job["status"], "succeeded")

            with manager.session_factory() as session:
                comment_count = session.execute(select(func.count(PublisherRawComment.id))).scalar_one()
                stored_sync_job = session.get(PublisherCommentSyncJob, sync_job["job_id"])
            self.assertEqual(comment_count, 1)
            self.assertEqual(stored_sync_job.status, "succeeded")

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
            self.assertEqual(session_sync["message"], "起点小说 浏览器会话已同步到后端。")
            self.assertTrue(session_sync["server_time"])
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
                create_if_missing=True,
                book_meta={
                    "audience": "male",
                    "primary_category": "都市日常",
                    "protagonist_names": ["韩砚", "林雾"],
                    "intro": "一段关于旧城与旧案的故事。",
                },
            )
            self.assertEqual(server_job["status"], "pending")
            self.assertEqual(server_job["message"], "起点小说 上传任务已创建，等待浏览器扩展执行。")
            self.assertTrue(server_job["result_payload"]["create_if_missing"])
            self.assertEqual(server_job["result_payload"]["book_meta"]["primary_category"], "都市日常")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_ingest_comments_batch_resolves_titles_once_per_batch(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()

        try:
            with manager.session_factory() as session:
                updater = StateUpdater(session)
                project = updater.create_project(title="测试书", premise="前提", genre="玄幻")
                session.commit()

            with capture_select_statements(engine) as select_statements:
                batch = manager.ingest_comments_batch(
                    client_id="client-1",
                    platform="fanqie",
                    comments=[
                        {
                            "remote_comment_id": f"comment-{index}",
                            "work_id": "book-1",
                            "work_name": "测试书",
                            "chapter_id": "chapter-1",
                            "chapter_title": "第一章",
                            "author_id": f"user-{index}",
                            "author_name": f"读者{index}",
                            "body": "催更",
                            "created_at": "2026-03-31T12:00:00Z",
                        }
                        for index in range(1, 4)
                    ],
                )

            with manager.session_factory() as session:
                stored_comments = session.execute(
                    select(PublisherRawComment)
                    .order_by(PublisherRawComment.remote_comment_id.asc())
                ).scalars().all()
        finally:
            engine.dispose()
            tmp.cleanup()

        self.assertEqual(batch["inserted"], 3)
        self.assertEqual(count_matching_statements(select_statements, " from projects"), 1)
        self.assertEqual(
            [comment.project_id for comment in stored_comments],
            [project.id, project.id, project.id],
        )

    def test_publisher_manager_keeps_browser_sessions_per_client_and_restores_latest_synced_one(self) -> None:
        tmp, engine, manager = self._make_publisher_manager(preferred_client_id="server-client")
        try:
            manager.record_browser_session(
                client_id="laptop-client",
                platform="qidian",
                cookies=[
                    {
                        "name": "AppAuthToken",
                        "value": "token-laptop",
                        "domain": ".write.qq.com",
                        "path": "/",
                    },
                    {
                        "name": "pubtoken",
                        "value": "pub-laptop",
                        "domain": ".write.qq.com",
                        "path": "/",
                    },
                ],
            )
            manager.record_browser_session(
                client_id="server-client",
                platform="qidian",
                cookies=[
                    {
                        "name": "AppAuthToken",
                        "value": "token-server",
                        "domain": ".write.qq.com",
                        "path": "/",
                    }
                ],
            )

            restored = manager.get_browser_session("qidian")
            assert restored is not None
            self.assertEqual(restored["client_id"], "server-client")
            self.assertEqual(restored["cookie_count"], 1)
            self.assertFalse(manager.has_browser_session("qidian"))

            with manager.session_factory() as session:
                entries = session.execute(
                    select(PublisherBrowserSessionEntry)
                    .where(PublisherBrowserSessionEntry.platform_id == "qidian")
                    .order_by(PublisherBrowserSessionEntry.client_id.asc())
                ).scalars().all()
                self.assertEqual([entry.client_id for entry in entries], ["laptop-client", "server-client"])
                summary = session.get(PublisherBrowserSession, "qidian")
                assert summary is not None
                self.assertEqual(summary.extension_client_id, "server-client")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_fanqie_session_cookie_alone_does_not_mark_connected(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            manager.record_browser_session(
                client_id="client-1",
                platform="fanqie",
                cookies=[
                    {
                        "name": "sessionid",
                        "value": "token",
                        "domain": ".fanqienovel.com",
                        "path": "/",
                    }
                ],
            )

            self.assertFalse(manager.has_browser_session("fanqie"))
            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertFalse(items["fanqie"]["connected"])
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

    def test_publisher_manager_claim_next_upload_job_resumes_same_client_running_job(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            job = manager.create_upload_job(
                platform="fanqie",
                book_name="测试书",
                chapter_title="续章",
                body="正文",
                upload_url=None,
                publish=False,
            )
            manager.update_upload_job_result(
                job_id=job["job_id"],
                client_id="client-1",
                status="running",
                message="浏览器扩展已接管任务",
                current_url="https://fanqienovel.com/main/writer/123/publish/456",
                error="",
                result_payload={"phase": "claimed"},
            )

            claimed = manager.claim_next_upload_job(
                client_id="client-1",
                connected_platforms=["fanqie"],
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["job_id"], job["job_id"])
            self.assertEqual(claimed["status"], "running")
            self.assertEqual(claimed["extension_client_id"], "client-1")
            self.assertEqual(claimed["current_url"], "https://fanqienovel.com/main/writer/123/publish/456")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_prefers_configured_linux_client_and_falls_back(self) -> None:
        tmp, engine, manager = self._make_publisher_manager(preferred_client_id="linux-client")
        try:
            manager.record_extension_heartbeat(
                client_id="linux-client",
                extension_version="0.1.0",
                browser_name="Chrome",
                browser_version="146.0",
                backend_base_url="http://10.0.0.150:8899",
                platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True, "login_method": "scan", "last_error": ""}],
            )
            manager.record_extension_heartbeat(
                client_id="laptop-client",
                extension_version="0.1.0",
                browser_name="Chrome",
                browser_version="146.0",
                backend_base_url="http://10.0.0.35:8899",
                platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True, "login_method": "scan", "last_error": ""}],
            )
            job = manager.create_upload_job(
                platform="fanqie",
                book_name="测试书",
                chapter_title="优先调度",
                body="正文",
                upload_url=None,
                publish=False,
            )

            self.assertIsNone(
                manager.claim_next_upload_job(
                    client_id="laptop-client",
                    connected_platforms=["fanqie"],
                )
            )
            claimed = manager.claim_next_upload_job(
                client_id="linux-client",
                connected_platforms=["fanqie"],
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["job_id"], job["job_id"])
            self.assertEqual(claimed["extension_client_id"], "linux-client")

            manager.update_upload_job_result(
                job_id=job["job_id"],
                client_id="linux-client",
                status="succeeded",
                message="完成",
                current_url="https://fanqienovel.com/main/writer/",
                error="",
                result_payload={},
            )
            fallback_job = manager.create_upload_job(
                platform="fanqie",
                book_name="测试书",
                chapter_title="回退调度",
                body="正文",
                upload_url=None,
                publish=False,
            )
            with manager.session_factory() as session:
                client = session.get(PublisherExtensionClient, "linux-client")
                assert client is not None
                client.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
                session.commit()

            fallback_claim = manager.claim_next_upload_job(
                client_id="laptop-client",
                connected_platforms=["fanqie"],
            )
            self.assertIsNotNone(fallback_claim)
            assert fallback_claim is not None
            self.assertEqual(fallback_claim["job_id"], fallback_job["job_id"])
            self.assertEqual(fallback_claim["extension_client_id"], "laptop-client")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_strict_preferred_client_blocks_fallback_claims(self) -> None:
        tmp, engine, manager = self._make_publisher_manager(
            preferred_client_id="linux-client",
            strict_preferred_client=True,
        )
        try:
            manager.record_extension_heartbeat(
                client_id="linux-client",
                extension_version="0.1.0",
                browser_name="Chrome",
                browser_version="146.0",
                backend_base_url="http://10.0.0.150:8899",
                platforms=[{"platform": "fanqie", "connected": False, "cookie_signal": False}],
            )
            manager.record_extension_heartbeat(
                client_id="laptop-client",
                extension_version="0.1.0",
                browser_name="Chrome",
                browser_version="146.0",
                backend_base_url="http://10.0.0.35:8899",
                platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True}],
            )
            job = manager.create_upload_job(
                platform="fanqie",
                book_name="测试书",
                chapter_title="严格首选调度",
                body="正文",
                upload_url=None,
                publish=False,
            )

            self.assertIsNone(
                manager.claim_next_upload_job(
                    client_id="laptop-client",
                    connected_platforms=["fanqie"],
                )
            )
            claimed = manager.claim_next_upload_job(
                client_id="linux-client",
                connected_platforms=["fanqie"],
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["job_id"], job["job_id"])
            self.assertEqual(claimed["extension_client_id"], "linux-client")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_list_platforms_falls_back_from_stale_preferred_client(self) -> None:
        tmp, engine, manager = self._make_publisher_manager(preferred_client_id="linux-client")
        try:
            manager.record_extension_heartbeat(
                client_id="linux-client",
                extension_version="0.1.0",
                browser_name="Chrome",
                browser_version="146.0",
                backend_base_url="http://10.0.0.150:8899",
                platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True, "login_method": "scan", "last_error": ""}],
            )
            manager.record_extension_heartbeat(
                client_id="laptop-client",
                extension_version="0.1.0",
                browser_name="Chrome",
                browser_version="146.0",
                backend_base_url="http://10.0.0.35:8899",
                platforms=[{"platform": "fanqie", "connected": True, "cookie_signal": True, "login_method": "scan", "last_error": ""}],
            )

            with manager.session_factory() as session:
                client = session.get(PublisherExtensionClient, "linux-client")
                platform_state = session.get(
                    PublisherExtensionPlatformState,
                    {"client_id": "linux-client", "platform_id": "fanqie"},
                )
                assert client is not None
                assert platform_state is not None
                stale_at = datetime.now(timezone.utc) - timedelta(minutes=10)
                client.last_heartbeat_at = stale_at
                platform_state.last_heartbeat_at = stale_at
                session.commit()

            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertFalse(items["fanqie"]["connected"])
            self.assertTrue(items["fanqie"]["extension_online"])
            self.assertEqual(items["fanqie"]["extension_client_id"], "linux-client")
            self.assertTrue(items["fanqie"]["fallback_available"])
            self.assertEqual(items["fanqie"]["fallback_client_id"], "laptop-client")
            self.assertTrue(items["fanqie"]["latest_client_state"]["connected"])
            self.assertTrue(items["fanqie"]["last_heartbeat_at"].endswith("PDT"))
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_heartbeat_does_not_trust_sticky_connected_without_cookie_signal(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
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
                        "cookie_signal": False,
                        "login_method": "scan",
                        "last_error": "",
                    }
                ],
            )

            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertFalse(items["qidian"]["connected"])
            with manager.session_factory() as session:
                state = session.get(PublisherConnectionState, "qidian")
                assert state is not None
                self.assertFalse(state.connected)
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publishers_page_and_extension_api_routes(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        old_config = api_module._config
        old_manager = api_module._publisher_manager
        try:
            api_module._config = Config(
                database_url=engine.url.render_as_string(hide_password=False),
                publisher_extension_api_key="secret",
                publisher_session_secret="test-session-secret",
                publisher_session_encryption_required=True,
                minimax_api_key="",
                minimax_model="fake-model",
                    chapter_review_form_mode="off",
            )
            api_module._publisher_manager = manager
            with TestClient(api_module.app) as client:
                page = client.get("/publishers")
                self.assertEqual(page.status_code, 200)
                self.assertIn("浏览器扩展", page.text)
                self.assertIn("首次安装请手动完成这几步", page.text)
                self.assertIn("browser_extension/forwin-publisher", page.text)
                self.assertIn("排队中与执行中的上传任务", page.text)
                self.assertIn("下载扩展包", page.text)
                self.assertIn("下载 Firefox 扩展包", page.text)
                self.assertIn("/api/publishers/extension-package", page.text)
                self.assertIn("/api/publishers/extension-package/firefox", page.text)

                package = client.get("/api/publishers/extension-package")
                self.assertEqual(package.status_code, 200)
                self.assertEqual(package.headers["content-type"], "application/zip")
                self.assertIn("forwin-publisher-extension.zip", package.headers["content-disposition"])
                self.assertGreater(len(package.content), 0)
                self.assertIn(b"manifest.json", package.content)

                firefox_package = client.get("/api/publishers/extension-package/firefox")
                self.assertEqual(firefox_package.status_code, 200)
                self.assertEqual(firefox_package.headers["content-type"], "application/zip")
                self.assertIn(
                    "forwin-publisher-firefox-extension.zip",
                    firefox_package.headers["content-disposition"],
                )
                self.assertEqual(firefox_package.headers["x-forwin-extension-target"], "firefox")
                self.assertGreater(len(firefox_package.content), 0)
                self.assertIn(b"manifest.json", firefox_package.content)

                firefox_payload = api_publisher_ops_module._build_extension_package(
                    Path.cwd() / "browser_extension" / "forwin-publisher",
                    target="firefox",
                )
                with zipfile.ZipFile(BytesIO(firefox_payload)) as archive:
                    manifest = json.loads(
                        archive.read("forwin-publisher-firefox/manifest.json").decode("utf-8")
                    )
                    self.assertIn("forwin-publisher-firefox/README.md", archive.namelist())
                self.assertIn("options_ui", manifest)
                self.assertNotIn("options_page", manifest)
                self.assertEqual(manifest["background"]["scripts"], ["background.js"])
                self.assertEqual(
                    manifest["browser_specific_settings"]["gecko"]["id"],
                    "forwin-publisher@example.com",
                )
                self.assertNotIn("debugger", manifest.get("permissions", []))

                platforms = client.get("/api/publishers/platforms")
                self.assertEqual(platforms.status_code, 200)
                self.assertEqual(len(platforms.json()), 2)
                self.assertEqual(platforms.json()[0]["supported_login_methods"], ["scan"])

                created = client.post(
                    "/api/publishers/upload-jobs",
                    json={
                        "platform": "fanqie",
                        "book_name": "测试书",
                        "chapter_title": "第一章",
                        "body": "正文",
                        "publish": False,
                        "create_if_missing": True,
                        "book_meta": {
                            "audience": "male",
                            "primary_category": "都市日常",
                            "protagonist_names": ["韩砚", "林雾"],
                            "intro": "一段关于旧城与旧案的故事。",
                        },
                    },
                )
                self.assertEqual(created.status_code, 200)
                job_id = created.json()["job_id"]
                self.assertEqual(created.json()["status"], "pending")
                self.assertTrue(created.json()["result_payload"]["create_if_missing"])
                self.assertEqual(created.json()["result_payload"]["book_meta"]["primary_category"], "都市日常")

                fetched = client.get(f"/api/publishers/upload-jobs/{job_id}")
                self.assertEqual(fetched.status_code, 200)
                self.assertEqual(fetched.json()["job_id"], job_id)

                listed = client.get("/api/publishers/upload-jobs")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(len(listed.json()), 1)
                self.assertEqual(listed.json()[0]["job_id"], job_id)

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
                self.assertEqual(heartbeat.json()["message"], "扩展心跳已记录。")
                self.assertTrue(heartbeat.json()["server_time"])

                with manager.session_factory() as session:
                    fanqie_state = session.get(PublisherConnectionState, "fanqie")
                    stored_session = session.get(PublisherBrowserSession, "fanqie")
                    assert fanqie_state is not None
                    self.assertFalse(fanqie_state.connected)
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

                restored_session = client.get(
                    "/api/publishers/extension/browser-sessions/qidian",
                    headers={"X-Forwin-Extension-Key": "secret"},
                )
                self.assertEqual(restored_session.status_code, 200)
                self.assertEqual(restored_session.json()["platform"], "qidian")
                self.assertEqual(restored_session.json()["cookie_count"], 2)
                self.assertEqual(restored_session.json()["cookies"][0]["name"], "AppAuthToken")

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
                self.assertEqual(server_claim.json()["status"], "pending")
                self.assertEqual(server_claim.json()["message"], "起点小说 上传任务已创建，等待浏览器扩展执行。")

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
                self.assertTrue(updated.json()["result_payload"]["create_if_missing"])
                self.assertEqual(updated.json()["result_payload"]["book_meta"]["primary_category"], "都市日常")
                self.assertEqual(updated.json()["result_payload"]["mode"], "draft")

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

                claimed_comment_job = client.post(
                    "/api/publishers/extension/comment-sync-jobs/claim",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "connected_platforms": ["fanqie"],
                    },
                )
                self.assertEqual(claimed_comment_job.status_code, 200)
                self.assertTrue(claimed_comment_job.json()["found"])
                self.assertEqual(claimed_comment_job.json()["job"]["job_id"], comment_job.json()["job_id"])
                self.assertEqual(claimed_comment_job.json()["job"]["status"], "running")

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

                finished_comment_job = client.post(
                    f"/api/publishers/comment-sync-jobs/{comment_job.json()['job_id']}/result",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "status": "succeeded",
                        "message": "评论同步已完成。",
                        "error": "",
                        "result_payload": {"fetched_count": 1},
                    },
                )
                self.assertEqual(finished_comment_job.status_code, 200)
                self.assertEqual(finished_comment_job.json()["status"], "succeeded")

                self.assertEqual(client.get("/publishers/qidian/auth").status_code, 404)
                self.assertEqual(client.post("/api/publishers/fanqie/login").status_code, 404)
                self.assertEqual(client.post("/api/publishers/upload", json={}).status_code, 404)
        finally:
            api_module._config = old_config
            api_module._publisher_manager = old_manager
            engine.dispose()
            tmp.cleanup()

    def test_home_page_renders_minimax_defaults_and_apikey_field(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                api_module._config = Config(
                    database_url=postgres_test_url(),
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
                with TestClient(api_module.app) as client:
                    page = client.get("/")

                    self.assertEqual(page.status_code, 200)
                    self.assertIn('id="model_form_api_key"', page.text)
                    self.assertIn("https://api.minimaxi.com/v1", page.text)
                    self.assertIn("MiniMax-M2.7", page.text)
                    self.assertIn("Kimi 中文站 / Moonshot.cn", page.text)
                    self.assertIn("https://api.moonshot.cn/v1", page.text)
                    self.assertIn("kimi-k2.5", page.text)
                    self.assertIn("任务", page.text)
                    self.assertIn("模型", page.text)
                    self.assertIn("Operation Mode", page.text)
                    self.assertIn("/publishers", page.text)
                    self.assertIn("模型设置", page.text)
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_publishers_page_uses_extension_bridge_flow(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            try:
                api_module._config = Config(
                    database_url=postgres_test_url("publishers-extension-flow"),
                    runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                )
                with TestClient(api_module.app) as client:
                    page = client.get("/publishers")
            finally:
                api_module._config = old_config

        self.assertEqual(page.status_code, 200)
        self.assertIn("forwin-publisher-extension", page.text)
        self.assertIn("open-login", page.text)
        self.assertIn("open-options", page.text)
        self.assertIn("浏览器扩展未响应", page.text)
        self.assertIn("let selectedPlatformId = '';", page.text)
        self.assertIn("const nextSelectedPlatformId = selectedPlatformId || select.value || '';", page.text)
        self.assertIn("selectedPlatformId = select.value || '';", page.text)
        self.assertIn("document.getElementById('platform').addEventListener('change'", page.text)

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
            db_path = postgres_test_url("review_api")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="checkpoint",
                )
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "Review API",
                        "setting_summary": "无",
                        "chapters": [
                            {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["第1章正文"]},
                            {"chapter_number": 2, "title": "第二章", "one_line": "延续", "goals": ["第2章正文"]},
                        ],
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = f"第{context.chapter_number}章正文" * 520
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
                    database_url=db_path,
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
                        api_module.ChapterReviewApproveRequest(
                            continue_generation=True,
                            reason="phase05 continue review",
                        ),
                    )
                    task_payload = api_module.get_task(approve_payload.task_id)

                self.assertEqual(review_payload.verdict, "pass")
                self.assertEqual(approve_payload.project_id, run_result.project_id)
                self.assertEqual(approve_payload.status, "accepted")
                self.assertEqual(approve_payload.task_id, task_payload.task_id)
                self.assertIn("已启动后续章节继续执行", approve_payload.message)
                self.assertEqual(task_payload.project_id, run_result.project_id)
                self.assertEqual(len(FakeThread.created), 0)
                self.assertEqual(task_payload.status, "queued")
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

        old_config = api_module._config
        old_engine = api_module._engine
        old_factory = api_module._SessionFactory
        old_tasks = api_module._tasks
        temp_engine = None
        try:
            api_module._config = Config(
                database_url=postgres_test_url(),
                minimax_api_key="",
                minimax_base_url="https://api.minimaxi.com/v1",
                minimax_model="MiniMax-M2.7",
            )
            temp_engine = get_engine(api_module._config.database_url)
            api_module._engine = temp_engine
            api_module._SessionFactory = get_session_factory(temp_engine)
            api_module._tasks = {}
            with TestClient(api_module.app) as client:
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
            self.assertEqual(len(FakeThread.created), 0)
            task = api_module._get_generation_task_or_404(response.json()["task_id"])
            overrides = task["execution_payload"]["runtime_overrides"]
            self.assertNotIn("minimax_api_key", overrides)
            self.assertEqual(overrides["minimax_base_url"], "https://example.test/v1")
            self.assertEqual(overrides["minimax_model"], "custom-model")
        finally:
            if temp_engine is not None:
                temp_engine.dispose()
            api_module._config = old_config
            api_module._engine = old_engine
            api_module._SessionFactory = old_factory
            api_module._tasks = old_tasks

    def test_generate_for_existing_project_keeps_book_context(self) -> None:
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
            db_path = postgres_test_url("existing-project-generate")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)
            with session_factory() as session:
                updater = StateUpdater(session)
                project = updater.create_project(
                    "测试书",
                    "前提",
                    "玄幻",
                    "设定",
                    creation_status="writing",
                )
                session.commit()

            old_config = api_module._config
            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            old_tasks = api_module._tasks
            try:
                api_module._config = Config(
                    database_url=db_path,
                    minimax_api_key="saved-key",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                api_module._engine = engine
                api_module._SessionFactory = session_factory
                api_module._tasks = {}
                with TestClient(api_module.app) as client:
                    with patch.object(api_module.threading, "Thread", FakeThread):
                        response = client.post(
                            "/api/generate",
                            json={
                                "project_id": project.id,
                                "premise": "前提",
                                "genre": "玄幻",
                                "num_chapters": 3,
                            },
                        )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["project_id"], project.id)
                self.assertEqual(payload["title"], "测试书")
                self.assertEqual(len(FakeThread.created), 0)
                task = api_module._get_generation_task_or_404(payload["task_id"])
                self.assertEqual(task["project_id"], project.id)
                self.assertEqual(task["execution_payload"]["mode"], "initial")
            finally:
                api_module._config = old_config
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory
                api_module._tasks = old_tasks
                engine.dispose()

    def test_run_generation_with_config_uses_existing_project_path_when_project_id_present(self) -> None:
        class FakeOrchestrator:
            def __init__(self, config, progress_callback=None, should_abort=None):
                self.config = config
                self.progress_callback = progress_callback
                self.should_abort = should_abort
                self.run_calls = []
                self.run_existing_calls = []
                self.llm_client = SimpleNamespace(close=lambda: None)
                self.engine = SimpleNamespace(dispose=lambda: None)

            def run(self, **kwargs):
                self.run_calls.append(kwargs)
                return RunResult(project_id="new-project", requested_chapters=3)

            def run_existing_project(self, project_id: str, *, num_chapters: int):
                self.run_existing_calls.append((project_id, num_chapters))
                return RunResult(project_id=project_id, requested_chapters=num_chapters)

        updates: list[dict[str, object]] = []

        def update_task(_task_id: str, **changes):
            updates.append(changes)

        config = Config(
            database_url=postgres_test_url(),
            minimax_api_key="saved-key",
            minimax_base_url="https://api.minimaxi.com/v1",
            minimax_model="MiniMax-M2.7",
        )
        fake = FakeOrchestrator(config)
        with patch.object(api_runtime_module, "WritingOrchestrator", return_value=fake):
            api_runtime_module.run_generation_with_config(
                "task-1",
                "前提",
                "玄幻",
                4,
                config,
                update_task,
                logging.getLogger("test"),
                project_id="book-1",
                should_abort=lambda: False,
        )

        self.assertEqual(fake.run_calls, [])
        self.assertEqual(fake.run_existing_calls, [("book-1", 4)])
        self.assertEqual(updates[0], {"status": "running"})
        self.assertEqual(
            updates[1],
            {
                "status": "completed",
                "project_id": "book-1",
                "failed_chapters": [],
                "paused_chapters": [],
                "frozen_artifacts": [],
            },
        )
        self.assertEqual(updates[2], {"message": "已完成 4 / 4 章"})

    def test_run_continue_project_with_config_passes_max_chapters(self) -> None:
        class FakeOrchestrator:
            def __init__(self, config, progress_callback=None, should_abort=None):
                self.config = config
                self.progress_callback = progress_callback
                self.should_abort = should_abort
                self.continue_calls = []
                self.llm_client = SimpleNamespace(close=lambda: None)
                self.engine = SimpleNamespace(dispose=lambda: None)

            def continue_project(
                self,
                project_id: str,
                max_chapters: int | None = None,
                resume_from_chapter: int | None = None,
            ):
                self.continue_calls.append((project_id, max_chapters))
                return RunResult(
                    project_id=project_id,
                    requested_chapters=6,
                    completed_chapters=[4, 5],
                )

        updates: list[dict[str, object]] = []

        def update_task(_task_id: str, **changes):
            updates.append(changes)

        config = Config(
            database_url=postgres_test_url(),
            minimax_api_key="saved-key",
            minimax_base_url="https://api.minimaxi.com/v1",
            minimax_model="MiniMax-M2.7",
        )
        fake = FakeOrchestrator(config)
        with patch.object(api_runtime_module, "WritingOrchestrator", return_value=fake):
            api_runtime_module.run_continue_project_with_config(
                "task-2",
                "book-2",
                config,
                update_task,
                logging.getLogger("test"),
                should_abort=lambda: False,
                max_chapters=2,
            )

        self.assertEqual(fake.continue_calls, [("book-2", 2)])
        self.assertEqual(
            updates[-1],
            {
                "message": "继续执行完成章节: 4, 5",
            },
        )

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
                database_url=postgres_test_url(),
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
                    api_module.ChapterReviewApproveRequest(
                        continue_generation=False,
                        reason="phase05 plain approve",
                    ),
                )

            self.assertEqual(payload.project_id, "proj-1")
            self.assertEqual(payload.chapter_number, 1)
            self.assertEqual(payload.status, "accepted")
            self.assertEqual(payload.message, "ok")
            self.assertEqual(fake.calls, [("proj-1", 1)])
        finally:
            api_module._config = old_config
            api_module._orchestrator = old_orchestrator
            api_module._tasks = old_tasks
            api_module._runtime_settings = old_store

    def test_get_engine_rejects_sqlite_database_urls(self) -> None:
        with self.assertRaises(ValueError):
            get_engine(("sqlite" + ":///tmp/forwin.db"))

    def test_llm_settings_api_persists_runtime_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                api_module._config = Config(
                    database_url=postgres_test_url(),
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
                with TestClient(api_module.app) as client:
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
                self.assertEqual(current.json()["min_chapter_chars"], 2500)
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_llm_settings_api_persists_runtime_modes(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                api_module._config = Config(
                    database_url=postgres_test_url(),
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
                with TestClient(api_module.app) as client:
                    saved = client.post(
                        "/api/settings/llm",
                        json={
                            "api_key": "",
                            "base_url": "https://api.minimaxi.com/v1",
                            "model": "MiniMax-M2.7",
                            "operation_mode": "checkpoint",
                            "freeze_failed_candidates": False,
                            "min_chapter_chars": 2800,
                        },
                    )
                    current = client.get("/api/settings/llm")

                self.assertEqual(saved.status_code, 200)
                self.assertEqual(current.status_code, 200)
                self.assertEqual(current.json()["operation_mode"], "checkpoint")
                self.assertEqual(current.json()["freeze_failed_candidates"], False)
                self.assertEqual(current.json()["min_chapter_chars"], 2800)
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_llm_profile_endpoints_and_preferences(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                api_module._config = Config(
                    database_url=postgres_test_url(),
                    runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                    minimax_api_key="",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                api_module._runtime_settings = RuntimeSettingsStore(
                    api_module._config.runtime_settings_path,
                    default_api_key="default-key",
                    default_base_url=api_module._config.minimax_base_url,
                    default_model=api_module._config.minimax_model,
                )
                saved_profile = api_module.save_llm_profile(
                    api_module.LLMProfileUpsertRequest(
                        name="OpenRouter 备用",
                        api_key="sk-alt",
                        base_url="https://openrouter.ai/api/v1",
                        model="openrouter/test",
                        set_as_default=True,
                    )
                )
                profile_id = saved_profile.default_profile_id

                prefs = api_module.save_llm_preferences(
                    api_module.LLMPreferencesRequest(
                        operation_mode="checkpoint",
                        freeze_failed_candidates=False,
                        min_chapter_chars=3100,
                    )
                )
                current = api_module.get_llm_settings()
                deleted = api_module.delete_llm_profile(profile_id)

                self.assertEqual(prefs.operation_mode, "checkpoint")
                self.assertEqual(current.default_profile_id, profile_id)
                self.assertEqual(current.operation_mode, "checkpoint")
                self.assertEqual(current.freeze_failed_candidates, False)
                self.assertEqual(current.min_chapter_chars, 3100)
                self.assertGreaterEqual(len(current.profiles), 2)
                self.assertNotEqual(deleted.default_profile_id, profile_id)
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_tasks_list_endpoint_returns_recent_items(self) -> None:
        old_tasks = api_module._tasks
        old_session_factory = api_module._SessionFactory
        now = datetime.now(timezone.utc)
        try:
            api_module._SessionFactory = None
            api_module._tasks = {
                "task-old": {
                    "status": "completed",
                    "project_id": "proj-old",
                    "error": None,
                    "message": "done",
                    "failed_chapters": [],
                    "paused_chapters": [],
                    "frozen_artifacts": [],
                    "created_at": now - timedelta(minutes=2),
                    "updated_at": now - timedelta(minutes=1),
                },
                "task-new": {
                    "status": "running",
                    "project_id": "proj-new",
                    "error": None,
                    "message": "running",
                    "failed_chapters": [],
                    "paused_chapters": [],
                    "frozen_artifacts": [],
                    "created_at": now,
                    "updated_at": now,
                },
            }
            payload = api_module.list_tasks(limit=10)
            self.assertEqual([item.task_id for item in payload], ["task-new", "task-old"])
            self.assertEqual(payload[0].status, "running")
            self.assertTrue(payload[0].created_at)
            self.assertTrue(payload[0].updated_at)
        finally:
            api_module._tasks = old_tasks
            api_module._SessionFactory = old_session_factory

    def test_project_delete_endpoint_cascades_related_rows(self) -> None:
        tmp = TemporaryDirectory()
        engine = get_engine(postgres_test_url("project-delete"))
        init_db(engine)
        session_factory = get_session_factory(engine)

        old_session_factory = api_module._SessionFactory
        try:
            with session_factory() as session:
                project = Project(id="proj-delete", title="待删项目", premise="premise", genre="玄幻")
                session.add(project)
                session.commit()
                arc = ArcPlanVersion(id="arc-delete", project_id=project.id, arc_synopsis="arc")
                plan = ChapterPlan(
                    id="plan-delete",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    status="completed",
                )
                session.add(arc)
                session.commit()
                session.add(plan)
                session.commit()
                draft = ChapterDraft(
                    id="draft-delete",
                    chapter_plan_id=plan.id,
                    version=1,
                    body_text="正文",
                    summary="摘要",
                    char_count=2,
                )
                review = ChapterReview(
                    id="review-delete",
                    draft_id=draft.id,
                    verdict="pass",
                )
                session.add_all([draft, review])
                session.commit()

            api_module._SessionFactory = session_factory
            response = api_module.delete_project("proj-delete")
            self.assertEqual(response.project_id, "proj-delete")
            self.assertEqual(response.message, "项目《待删项目》已删除。")
            with session_factory() as session:
                self.assertIsNone(session.get(Project, "proj-delete"))
                self.assertEqual(
                    session.execute(select(func.count()).select_from(ChapterPlan)).scalar_one(),
                    0,
                )
                self.assertEqual(
                    session.execute(select(func.count()).select_from(ChapterDraft)).scalar_one(),
                    0,
                )
                self.assertEqual(
                    session.execute(select(func.count()).select_from(ChapterReview)).scalar_one(),
                    0,
                )
        finally:
            api_module._SessionFactory = old_session_factory
            engine.dispose()
            tmp.cleanup()

    def test_project_chapter_upload_job_uses_generated_chapter_body(self) -> None:
        tmp = TemporaryDirectory()
        engine = get_engine(postgres_test_url("project-upload"))
        init_db(engine)
        session_factory = get_session_factory(engine)
        manager = PublisherManager(session_factory, extension_api_key="secret")

        old_session_factory = api_module._SessionFactory
        old_manager = api_module._publisher_manager
        try:
            with session_factory() as session:
                project = Project(id="proj-upload", title="雾港潮生录", premise="premise", genre="悬疑")
                session.add(project)
                session.commit()
                arc = ArcPlanVersion(id="arc-upload", project_id=project.id, arc_synopsis="arc")
                plan = ChapterPlan(
                    id="plan-upload",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=3,
                    title="第三章 雨夜回港",
                    status="completed",
                )
                session.add(arc)
                session.commit()
                session.add(plan)
                session.commit()
                draft = ChapterDraft(
                    id="draft-upload",
                    chapter_plan_id=plan.id,
                    version=1,
                    body_text="这里是第三章正文",
                    summary="摘要",
                    char_count=8,
                )
                session.add(draft)
                session.commit()

            api_module._SessionFactory = session_factory
            api_module._publisher_manager = manager
            payload = api_module.create_project_chapter_upload_job(
                "proj-upload",
                api_module.ProjectChapterPublishRequest(
                    platform="fanqie",
                    chapter_number=3,
                    book_name="雾港潮生录",
                    publish=False,
                ),
            )

            self.assertEqual(payload.chapter_title, "第三章 雨夜回港")
            self.assertEqual(payload.body, "这里是第三章正文")
            self.assertEqual(payload.status, "pending")
        finally:
            api_module._SessionFactory = old_session_factory
            api_module._publisher_manager = old_manager
            engine.dispose()
            tmp.cleanup()

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
            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            old_tasks = api_module._tasks
            temp_engine = None
            try:
                api_module._config = Config(
                    database_url=postgres_test_url(),
                    runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                    minimax_api_key="",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                temp_engine = get_engine(api_module._config.database_url)
                api_module._engine = temp_engine
                api_module._SessionFactory = get_session_factory(temp_engine)
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
                with TestClient(api_module.app) as client:
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
                self.assertEqual(len(FakeThread.created), 0)
                task = api_module._get_generation_task_or_404(response.json()["task_id"])
                overrides = task["execution_payload"]["runtime_overrides"]
                self.assertNotIn("minimax_api_key", overrides)
                self.assertEqual(overrides["minimax_base_url"], "https://stored.example/v1")
                self.assertEqual(overrides["minimax_model"], "stored-model")
            finally:
                if temp_engine is not None:
                    temp_engine.dispose()
                api_module._config = old_config
                api_module._runtime_settings = old_store
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory
                api_module._tasks = old_tasks

    def test_phase24_persists_experience_overlay_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("overlay")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="checkpoint",
                )
            )
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "Overlay 测试",
                    "setting_summary": "无",
                    "chapters": [
                        {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]},
                        {"chapter_number": 2, "title": "第二章", "one_line": "升级", "goals": ["兑现回报"]},
                    ],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }
                orchestrator.arc_director.draft_arc_structure = lambda **kwargs: {
                    "phase_layout": ["setup", "pressure", "payoff"],
                    "key_beats": ["开场受压", "第一次反击", "留下悬念"],
                    "thread_priorities": [{"name": "主线", "priority": 1, "reason": "核心推进"}],
                    "hotspot_candidates": ["第一次反击"],
                    "compression_candidates": [],
                    "reader_promise": {
                        "pleasure_promise": "稳定给出悬念与反击快感",
                        "acceptable_drag_level": "low",
                        "acceptable_exposition_density": "medium",
                        "cliffhanger_aggressiveness": "high",
                    },
                    "arc_payoff_map": {
                        "macro_payoffs": [
                            {
                                "payoff_id": "p1",
                                "category": "mystery",
                                "template_id": "mystery-locked-clue",
                                "target_chapter_hint": "band-mid",
                                "setup_requirement": "先埋异象",
                                "success_signal": "读者确认有真相在逼近",
                            }
                        ],
                        "awe_kit": ["异象", "反转"],
                    },
                }
                orchestrator.writer.write_chapter = lambda context: WriterOutput(
                    chapter_number=context.chapter_number,
                    title=f"第{context.chapter_number}章",
                    body="正文" * 800,
                    char_count=1600,
                    end_of_chapter_summary="ok",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )
                orchestrator.run("p", "g", 2)

                session = get_session_factory(get_engine(db_path))()
                try:
                    structure = session.execute(select(ArcStructureDraft)).scalar_one()
                    band_plan = session.execute(select(BandExperiencePlan)).scalar_one()
                    chapter_plan = session.execute(
                        select(ChapterPlan).where(ChapterPlan.chapter_number == 1)
                    ).scalar_one()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertIn("pleasure_promise", json.loads(structure.reader_promise_json))
            self.assertIn("macro_payoffs", json.loads(structure.arc_payoff_map_json))
            self.assertGreaterEqual(band_plan.stall_guard_max_gap, 1)
            self.assertEqual(
                {"confirmation", "constraint", "reversal"},
                {
                    item["payoff_type"]
                    for item in json.loads(band_plan.schedule_json)["ambiguity_payoffs"]
                },
            )
            self.assertIn("planned_reward_tags", json.loads(chapter_plan.experience_plan_json))

    def test_scene_generation_parser_supports_experience_fields(self) -> None:
        writer = ChapterWriter(llm_client=SimpleNamespace(chat=lambda *args, **kwargs: {}))
        writer._chat_preview_text = lambda *args, **kwargs: (
            "<<FORWIN_BODY>>\n"
            "scene body\n"
            "<<FORWIN_SUMMARY>>\n"
            "summary\n"
            "<<FORWIN_TIME>>\n"
            "夜里\n"
            "<<FORWIN_LOCATION>>\n"
            "废楼\n"
            "<<FORWIN_ENTITIES>>\n"
            "林夜\n"
            "<<FORWIN_REWARD>>\n"
            "mystery\n"
            "<<FORWIN_IMMERSION>>\n"
            "雨声压着窗框\n"
            "<<FORWIN_PROGRESS>>\n"
            "确认第一条线索"
        )
        context = ChapterContextPack(
            project_title="测试书",
            premise="前提",
            genre="玄幻",
            setting_summary="设定",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="开场",
            chapter_goals=["推进主线"],
        )
        scene = writer._generate_scene(
            context,
            ScenePlan(
                scene_no=1,
                objective="推进",
                reward_beat_tag="power",
                immersion_anchor="默认锚点",
                progress_marker="默认进展",
            ),
        )
        self.assertEqual(scene.reward_beat_tag, "mystery")
        self.assertEqual(scene.immersion_anchor, "雨声压着窗框")
        self.assertEqual(scene.progress_marker, "确认第一条线索")

    def test_copilot_fail_runs_three_rewrites_then_pauses(self) -> None:
        class AlwaysFailReviewHub:
            def __init__(self):
                self.calls = 0

            def review(self, **kwargs):
                self.calls += 1
                return ReviewVerdict(
                    verdict="fail",
                    issues=[
                        ContinuityIssue(
                            rule_name="reward_delivery_miss",
                            severity="error",
                            description="奖励未兑现",
                            reviewer="webnovel_experience",
                            issue_type="payoff_miss",
                            target_scope="scene",
                            evidence_refs=["planned_reward_tags=['mystery']"],
                        )
                    ],
                    repair_instruction=RepairInstruction(
                        repair_scope="scene",
                        failure_type="payoff_miss",
                        must_fix=["奖励未兑现"],
                        must_preserve=["第一章", "开场"],
                        design_patch={"planned_reward_tags": ["mystery"]},
                        evidence_refs=["planned_reward_tags=['mystery']"],
                    ),
                )

        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("copilot-rewrite")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="copilot",
                )
            )
            apply_calls = {"count": 0}
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "重写回路",
                    "setting_summary": "无",
                    "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }
                orchestrator.writer.write_chapter = lambda context: WriterOutput(
                    chapter_number=context.chapter_number,
                    title=f"第{context.chapter_number}章",
                    body="正文" * 900,
                    char_count=1800,
                    end_of_chapter_summary="ok",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )
                orchestrator.review_hub = AlwaysFailReviewHub()
                original_apply = orchestrator._apply_canon_candidate

                def record_apply(**kwargs):
                    apply_calls["count"] += 1
                    return original_apply(**kwargs)

                orchestrator._apply_canon_candidate = record_apply
                result = orchestrator.run("p", "g", 1)

                session = get_session_factory(get_engine(db_path))()
                try:
                    attempts = session.execute(select(ChapterRewriteAttempt)).scalars().all()
                    plan = session.execute(select(ChapterPlan)).scalar_one()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(result.paused_chapters, [1])
            self.assertEqual(len(attempts), 0)
            self.assertEqual(plan.status, "needs_review")
            self.assertEqual(apply_calls["count"], 0)

    def test_blackbox_exhausted_soft_fail_force_accepts_after_retry_budget(self) -> None:
        class SoftFailReviewHub:
            def __init__(self):
                self.calls = 0

            def review(self, **kwargs):
                self.calls += 1
                issue_specs = [
                    ("hook_soft", "章末钩子偏软", "hook_failure", "scene", {"hook_type": "hard_cliffhanger"}),
                    ("progress_stall", "推进仍偏慢", "stall", "band", {"progress_markers": ["推进主线"]}),
                    ("immersion_anchor_missing", "沉浸感仍偏弱", "immersion", "band", {"immersion_anchors": ["雨声压着窗框"]}),
                    ("reward_delivery_thin", "回报仍偏薄", "payoff_miss", "scene", {"planned_reward_tags": ["mystery", "emotion"]}),
                    ("progress_stall_late", "后段推进仍偏慢", "stall", "band", {"progress_markers": ["主角主动推进"]}),
                    ("immersion_anchor_late", "后段沉浸锚仍偏弱", "immersion", "scene", {"immersion_anchors": ["金属门低鸣"]}),
                    ("hook_soft_final", "最终残留软钩子", "hook_failure", "scene", {"hook_type": "hard_cliffhanger"}),
                ]
                rule_name, description, issue_type, target_scope, design_patch = issue_specs[min(self.calls - 1, len(issue_specs) - 1)]
                return ReviewVerdict(
                    verdict="fail",
                    issues=[
                        ContinuityIssue(
                            rule_name=rule_name,
                            severity="error",
                            description=description,
                            reviewer="webnovel_experience",
                            issue_type=issue_type,
                            target_scope=target_scope,
                            evidence_refs=[f"repair-call={self.calls}"],
                        )
                    ],
                    repair_instruction=RepairInstruction(
                        repair_scope="scene" if self.calls == 1 else "band" if self.calls == 2 else "arc",
                        failure_type=issue_type,  # type: ignore[arg-type]
                        must_fix=[description],
                        must_preserve=["第一章", "开场"],
                        design_patch=design_patch,
                        evidence_refs=[f"repair-call={self.calls}"],
                    ),
                )

        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("blackbox-force")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="blackbox",
                )
            )
            apply_calls = {"count": 0}
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "强制接受",
                    "setting_summary": "无",
                    "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }
                orchestrator.writer.write_chapter = lambda context: WriterOutput(
                    chapter_number=context.chapter_number,
                    title=f"第{context.chapter_number}章",
                    body="正文" * 900,
                    char_count=1800,
                    end_of_chapter_summary="ok",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )
                orchestrator.review_hub = SoftFailReviewHub()

                def record_apply(**kwargs):
                    apply_calls["count"] += 1
                    return None

                orchestrator._apply_canon_candidate = record_apply
                result = orchestrator.run("p", "g", 1)

                session = get_session_factory(get_engine(db_path))()
                try:
                    attempts = session.execute(
                        select(ChapterRewriteAttempt).order_by(ChapterRewriteAttempt.attempt_no)
                    ).scalars().all()
                    latest_draft = session.execute(
                        select(ChapterDraft).order_by(ChapterDraft.version.desc()).limit(1)
                    ).scalar_one()
                    review = session.execute(
                        select(ChapterReview)
                        .where(ChapterReview.draft_id == latest_draft.id)
                        .order_by(ChapterReview.created_at.desc())
                        .limit(1)
                    ).scalar_one()
                    plan = session.execute(select(ChapterPlan)).scalar_one()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "paused")
            self.assertEqual(len(attempts), 6)
            self.assertEqual(
                [item.repair_scope for item in attempts],
                ["draft", "draft", "chapter_plan", "chapter_plan", "band_plan", "band_plan"],
            )
            self.assertTrue(attempts[-1].forced_accept_applied)
            review_meta = json.loads(review.review_meta_json)
            self.assertTrue(review_meta.get("forced_accept_applied"))
            self.assertEqual((review_meta.get("final_gate_decision") or {}).get("decision"), "force_accept")
            self.assertEqual(plan.status, "accepted")
            self.assertEqual(plan.acceptance_mode, "force_accept_after_repair")
            self.assertEqual(plan.repair_attempt_count, 6)
            self.assertEqual(plan.canon_risk_level, "low")
            self.assertEqual(apply_calls["count"], 1)

    def test_historical_review_hub_merges_continuity_and_experience_repairs(self) -> None:
        hub = HistoricalReviewHub(experience_review_enabled=True, lint_review_enabled=False)
        context = ChapterContextPack(
            project_title="测试书",
            premise="前提",
            genre="玄幻",
            setting_summary="设定",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="开场",
            chapter_goals=["推进主线"],
        )
        writer_output = WriterOutput(
            chapter_number=1,
            title="第一章",
            body="正文" * 500,
            char_count=1000,
            end_of_chapter_summary="ok",
            state_changes=[],
            new_events=[],
            thread_beats=[],
            time_advance=None,
        )

        class FakeChecker:
            def check(self, project_id: str, output: WriterOutput) -> ReviewVerdict:
                return ReviewVerdict(
                    verdict="fail",
                    issues=[
                        ContinuityIssue(
                            rule_name="dead_character_active",
                            severity="error",
                            description="死人仍在行动",
                            reviewer="continuity",
                            issue_type="continuity",
                            target_scope="scene",
                            evidence_refs=["event=死人出手"],
                        )
                    ],
                )

        hub.experience_reviewer.review = lambda context, writer_output: ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="progress_stall",
                    severity="error",
                    description="推进停滞",
                    reviewer="webnovel_experience",
                    issue_type="stall",
                    target_scope="band",
                    evidence_refs=["thread=主线无推进"],
                )
            ],
            repair_instruction=RepairInstruction(
                repair_scope="band",
                failure_type="stall",
                must_fix=["推进停滞"],
                must_preserve=["第一章"],
                design_patch={"progress_markers": ["推进主线"]},
                evidence_refs=["thread=主线无推进"],
            ),
        )

        review = hub.review(
            project_id="p1",
            context=context,
            writer_output=writer_output,
            continuity_checker=FakeChecker(),
        )

        self.assertEqual(review.verdict, "fail")
        self.assertIsNotNone(review.repair_instruction)
        assert review.repair_instruction is not None
        self.assertEqual(review.repair_instruction.repair_scope, "band")
        self.assertEqual(review.repair_instruction.failure_type, "mixed")
        self.assertIn("死人仍在行动", review.repair_instruction.must_fix)
        self.assertIn("推进停滞", review.repair_instruction.must_fix)

    def test_copilot_rewrite_writer_error_pauses_instead_of_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("copilot-rewrite-error")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="copilot",
                )
            )
            calls = {"count": 0}
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "rewrite error",
                    "setting_summary": "无",
                    "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }

                def flaky_writer(context):
                    calls["count"] += 1
                    if calls["count"] == 1:
                        return WriterOutput(
                            chapter_number=context.chapter_number,
                            title="第一章",
                            body="正文" * 900,
                            char_count=1800,
                            end_of_chapter_summary="ok",
                            state_changes=[],
                            new_events=[],
                            thread_beats=[],
                            time_advance=None,
                        )
                    raise RuntimeError("rewrite writer exploded")

                orchestrator.writer.write_chapter = flaky_writer
                orchestrator.review_hub.review = lambda **kwargs: ReviewVerdict(
                    verdict="fail",
                    issues=[
                        ContinuityIssue(
                            rule_name="progress_stall",
                            severity="error",
                            description="推进停滞",
                            reviewer="webnovel_experience",
                            issue_type="stall",
                            target_scope="band",
                            evidence_refs=["thread=主线无推进"],
                        )
                    ],
                    repair_instruction=RepairInstruction(
                        repair_scope="band",
                        failure_type="stall",
                        must_fix=["推进停滞"],
                        must_preserve=["第一章"],
                        design_patch={"progress_markers": ["推进主线"]},
                        evidence_refs=["thread=主线无推进"],
                    ),
                )

                result = orchestrator.run("p", "g", 1)
                session = get_session_factory(get_engine(db_path))()
                try:
                    attempts = session.execute(select(ChapterRewriteAttempt)).scalars().all()
                    plan = session.execute(select(ChapterPlan)).scalar_one()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(plan.status, "needs_review")
            self.assertEqual(len(attempts), 0)

    def test_blackbox_rewrite_writer_error_reaches_manual_review_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("blackbox-rewrite-error")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                    operation_mode="blackbox",
                )
            )
            calls = {"count": 0}
            apply_calls = {"count": 0}
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "rewrite error",
                    "setting_summary": "无",
                    "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }

                def flaky_writer(context):
                    calls["count"] += 1
                    if calls["count"] == 1:
                        return WriterOutput(
                            chapter_number=context.chapter_number,
                            title="第一章",
                            body="正文" * 900,
                            char_count=1800,
                            end_of_chapter_summary="ok",
                            state_changes=[],
                            new_events=[],
                            thread_beats=[],
                            time_advance=None,
                        )
                    raise RuntimeError("rewrite writer exploded")

                orchestrator.writer.write_chapter = flaky_writer
                orchestrator.review_hub.review = lambda **kwargs: ReviewVerdict(
                    verdict="fail",
                    issues=[
                        ContinuityIssue(
                            rule_name="progress_stall",
                            severity="error",
                            description="推进停滞",
                            reviewer="webnovel_experience",
                            issue_type="stall",
                            target_scope="band",
                            evidence_refs=["thread=主线无推进"],
                        )
                    ],
                    repair_instruction=RepairInstruction(
                        repair_scope="band",
                        failure_type="stall",
                        must_fix=["推进停滞"],
                        must_preserve=["第一章"],
                        design_patch={"progress_markers": ["推进主线"]},
                        evidence_refs=["thread=主线无推进"],
                    ),
                )
                orchestrator._apply_canon_candidate = lambda **kwargs: apply_calls.__setitem__("count", apply_calls["count"] + 1) or None

                result = orchestrator.run("p", "g", 1)
                session = get_session_factory(get_engine(db_path))()
                try:
                    attempts = session.execute(select(ChapterRewriteAttempt)).scalars().all()
                    latest_draft = session.execute(select(ChapterDraft).order_by(ChapterDraft.version.desc()).limit(1)).scalar_one()
                    latest_review = session.execute(
                        select(ChapterReview).where(ChapterReview.draft_id == latest_draft.id).limit(1)
                    ).scalar_one()
                    plan = session.execute(select(ChapterPlan)).scalar_one()
                    checkpoint = session.execute(
                        select(BandCheckpoint)
                        .where(BandCheckpoint.project_id == plan.project_id)
                        .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                finally:
                    session.close()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "needs_review")
            self.assertEqual(plan.status, "needs_review")
            self.assertIsNone(checkpoint)
            self.assertEqual(len(attempts), 6)
            ordered_attempts = sorted(attempts, key=lambda item: item.attempt_no)
            self.assertEqual(
                [item.repair_scope for item in ordered_attempts],
                ["draft", "draft", "chapter_plan", "chapter_plan", "band_plan", "band_plan"],
            )
            self.assertFalse(any(item.forced_accept_applied for item in attempts))
            review_meta = json.loads(latest_review.review_meta_json)
            self.assertFalse(review_meta.get("forced_accept_applied"))
            self.assertEqual((review_meta.get("final_gate_decision") or {}).get("decision"), "manual_review_required")
            self.assertEqual(apply_calls["count"], 0)

    def test_wener_falls_back_to_heuristics_when_llm_json_is_invalid(self) -> None:
        from forwin.protocol.context import ReviewContextPack
        from forwin.protocol.experience import (
            BandDelightSchedule,
            BandRewardItem,
            ChapterExperiencePlan,
        )
        from forwin.reviewer.webnovel import WebNovelExperienceReviewer

        class FakeLLM:
            def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return "not-json-at-all"

        reviewer = WebNovelExperienceReviewer(
            enabled=True,
            llm_client=FakeLLM(),
            llm_enabled=True,
        )
        context = ReviewContextPack(
            project_title="测试书",
            chapter_number=3,
            chapter_plan_title="第三章",
            chapter_plan_one_line="主角发现新的真相",
            chapter_goals=["确认一条线索", "推进主线"],
            chapter_experience_plan=ChapterExperiencePlan(
                planned_reward_tags=["mystery", "power"],
                question_hook="幕后黑手是谁",
                question_resolution="确认有第三方势力介入",
                rule_anchors=["异象只能在付出代价后触发"],
                progress_markers=["主角确认一条真线索"],
                minimum_progress_channels=["event", "thread", "rule"],
            ),
            band_delight_schedule=BandDelightSchedule(
                band_id="band:1:3",
                chapter_start=1,
                chapter_end=3,
                scheduled_rewards=[
                    BandRewardItem(
                        chapter_hint=1,
                        category="power",
                        template_id="power-hidden-edge",
                        intent="micro_progress_power",
                    ),
                    BandRewardItem(
                        chapter_hint=2,
                        category="social",
                        template_id="social-face-slap",
                        intent="social_dominance",
                    ),
                    BandRewardItem(
                        chapter_hint=3,
                        category="mystery",
                        template_id="mystery-locked-clue",
                        intent="mystery_clue_or_reveal",
                    )
                ],
                stall_guard_max_gap=1,
            ),
        )
        writer_output = WriterOutput(
            chapter_number=3,
            title="第三章",
            body="他终于意识到这一切并非偶然。下一刻，门外传来了更危险的脚步声。",
            char_count=40,
            end_of_chapter_summary="主角确认了幕后另有其人，但更大的危险已经逼近。",
            new_events=[
                EventCandidate(
                    summary="主角确认黑雾异象背后还有第三方操盘者",
                    significance="major",
                    involved_entity_names=["主角"],
                )
            ],
            thread_beats=[
                ThreadBeatCandidate(
                    thread_name="主线",
                    beat_type="twist",
                    description="真相逼近但危机升级",
                )
            ],
            state_changes=[],
            time_advance=None,
        )

        verdict = reviewer.review(context, writer_output)

        self.assertIn(verdict.verdict, {"pass", "warn"})
        self.assertEqual(verdict.planned_reward_tags, ["mystery", "power"])
        self.assertIn("narrative_understanding", verdict.experience_scores)
        self.assertIn("stall_tolerance", verdict.experience_scores)
        self.assertTrue(verdict.review_notes)
        self.assertEqual(verdict.reviewer_mode, "heuristic_fallback")

    def test_wener_repairs_llm_json_and_rejects_unanchored_evidence(self) -> None:
        from forwin.protocol.context import ReviewContextPack
        from forwin.protocol.experience import ChapterExperiencePlan
        from forwin.reviewer.webnovel import WebNovelExperienceReviewer

        class RepairingLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                self.calls += 1
                if self.calls == 1:
                    return json.dumps(
                        {
                            "verdict": "warn",
                            "planned_reward_tags": ["mystery"],
                            "delivered_reward_tags": [],
                            "experience_scores": {
                                "narrative_understanding": 0.7,
                                "attentional_focus": 0.6,
                                "emotional_engagement": 0.4,
                                "narrative_presence": 0.6,
                                "payoff_delivery": 0.3,
                                "stall_tolerance": 0.6,
                                "hook_efficiency": 0.5,
                            },
                            "issues": [
                                {
                                    "rule_name": "bad_ref",
                                    "severity": "warning",
                                    "description": "证据引用非法",
                                    "issue_type": "payoff_miss",
                                    "target_scope": "chapter",
                                    "evidence_refs": ["not-in-pack"],
                                    "suggested_fix": "修复证据",
                                }
                            ],
                            "review_notes": ["需要修复"],
                            "repair_instruction": None,
                            "evidence_refs": ["not-in-pack"],
                            "review_summary": "bad",
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "verdict": "warn",
                        "planned_reward_tags": ["mystery"],
                        "delivered_reward_tags": [],
                        "experience_scores": {
                            "narrative_understanding": 0.7,
                            "attentional_focus": 0.6,
                            "emotional_engagement": 0.4,
                            "narrative_presence": 0.6,
                            "payoff_delivery": 0.3,
                            "stall_tolerance": 0.6,
                            "hook_efficiency": 0.5,
                        },
                        "issues": [
                            {
                                "rule_name": "anchored_ref",
                                "severity": "warning",
                                "description": "证据引用已修复",
                                "issue_type": "payoff_miss",
                                "target_scope": "chapter",
                                "evidence_refs": ["overlay:chapter_experience_plan"],
                                "suggested_fix": "按计划补 reward",
                            }
                        ],
                        "review_notes": ["已修复"],
                        "repair_instruction": None,
                        "evidence_refs": ["overlay:chapter_experience_plan"],
                        "review_summary": "ok",
                    },
                    ensure_ascii=False,
                )

        llm = RepairingLLM()
        reviewer = WebNovelExperienceReviewer(enabled=True, llm_client=llm, llm_enabled=True)
        context = ReviewContextPack(
            project_title="测试书",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="出现谜题",
            chapter_goals=["提出问题"],
            chapter_experience_plan=ChapterExperiencePlan(
                planned_reward_tags=["mystery"],
                question_hook="门后是谁",
            ),
        )
        writer_output = WriterOutput(
            chapter_number=1,
            title="第一章",
            body="门后传来声音。",
            char_count=7,
            end_of_chapter_summary="门后传来声音。",
        )

        verdict = reviewer.review(context, writer_output)

        self.assertEqual(llm.calls, 2)
        self.assertEqual(verdict.verdict, "warn")
        self.assertEqual(verdict.issues[0].evidence_refs, ["overlay:chapter_experience_plan"])
        self.assertEqual(verdict.reviewer_mode, "llm")

    def test_wener_promotes_confirmed_signals_to_evidence_but_rejects_audience_only_fail(self) -> None:
        from forwin.protocol.context import ReaderFeedbackView, ReviewContextPack, SignalSummaryView
        from forwin.protocol.experience import ChapterExperiencePlan
        from forwin.reviewer.webnovel import WebNovelExperienceReviewer

        class AudienceOnlyFailLLM:
            def __init__(self):
                self.prompts = []

            def chat(self, messages, **kwargs):  # noqa: ANN002, ANN003
                self.prompts.append(messages)
                return json.dumps(
                    {
                        "verdict": "fail",
                        "planned_reward_tags": ["mystery"],
                        "delivered_reward_tags": [],
                        "experience_scores": {
                            "narrative_understanding": 0.5,
                            "attentional_focus": 0.5,
                            "emotional_engagement": 0.5,
                            "narrative_presence": 0.5,
                            "payoff_delivery": 0.1,
                            "stall_tolerance": 0.2,
                            "hook_efficiency": 0.4,
                        },
                        "issues": [
                            {
                                "rule_name": "audience_only_fail",
                                "severity": "error",
                                "description": "只凭读者信号判 fail",
                                "issue_type": "payoff_miss",
                                "target_scope": "chapter",
                                "evidence_refs": ["audience_signal:pacing:arc:节奏"],
                                "suggested_fix": "补回报",
                            }
                        ],
                        "review_notes": ["读者说节奏慢"],
                        "repair_instruction": None,
                        "evidence_refs": ["audience_signal:pacing:arc:节奏"],
                        "review_summary": "bad",
                    },
                    ensure_ascii=False,
                )

        llm = AudienceOnlyFailLLM()
        reviewer = WebNovelExperienceReviewer(enabled=True, llm_client=llm, llm_enabled=True)
        context = ReviewContextPack(
            project_title="测试书",
            chapter_number=2,
            chapter_plan_title="第二章",
            chapter_plan_one_line="继续铺垫",
            chapter_goals=["保持悬念"],
            reader_feedback=ReaderFeedbackView(
                confirmed_signals=[
                    SignalSummaryView(
                        signal_key="pacing:arc:节奏",
                        signal_type="pacing",
                        target_name="节奏",
                        level="confirmed",
                        hit_count=4,
                        max_severity=3,
                    )
                ]
            ),
            chapter_experience_plan=ChapterExperiencePlan(planned_reward_tags=["mystery"]),
        )
        writer_output = WriterOutput(
            chapter_number=2,
            title="第二章",
            body="他推开门，发现线索仍然指向更深的雨夜。",
            char_count=20,
            end_of_chapter_summary="线索继续延伸。",
        )

        verdict = reviewer.review(context, writer_output)

        prompt_text = json.dumps(llm.prompts[0], ensure_ascii=False)
        self.assertIn("audience_signal:pacing:arc:节奏", prompt_text)
        self.assertEqual(verdict.reviewer_mode, "heuristic_fallback")
        self.assertIn("audience_signal:pacing:arc:节奏", verdict.confirmed_signal_refs)

    def test_project_arc_snapshot_payload_exposes_v27_overlay_fields(self) -> None:
        from forwin.api_project_payloads import project_arc_snapshot_payload

        payload = project_arc_snapshot_payload(
            latest_arc_envelope=None,
            latest_arc_analysis=None,
            latest_provisional=None,
            latest_arc_structure=SimpleNamespace(
                reader_promise_json=json.dumps({"genre_promise": "悬疑网文"}),
                arc_payoff_map_json=json.dumps(
                    {
                        "revelation_layers": [
                            {"layer_id": "rule-1", "layer_type": "rule", "summary": "规则显影", "chapter_window": "mid"}
                        ]
                    }
                ),
            ),
            latest_band_experience=SimpleNamespace(
                schedule_json=json.dumps(
                    {
                        "scheduled_rewards": [
                            {"category": "power", "template_id": "power-hidden-edge"},
                            {"category": "mystery", "template_id": "mystery-locked-clue"},
                        ],
                        "stall_guard_max_gap": 1,
                        "curiosity_beats": [
                            {
                                "chapter_hint": 1,
                                "question_open": "谁在背后布局",
                                "question_resolve": "确认不是偶然",
                                "escalated_question": "真正的规则边界是什么",
                            }
                        ],
                    }
                ),
                stall_guard_max_gap=1,
            ),
        )

        self.assertEqual(payload["active_reader_promise"]["genre_promise"], "悬疑网文")
        self.assertEqual(payload["active_revelation_layers"][0]["layer_id"], "rule-1")
        self.assertEqual(payload["active_band_curiosity_beats"][0]["question_open"], "谁在背后布局")
        self.assertEqual(payload["active_band_template_ids"], ["power-hidden-edge", "mystery-locked-clue"])

    def test_chapter_review_api_exposes_v27_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("review-detail-v27")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)
            with session_factory() as session:
                updater = StateUpdater(session)
                project = updater.create_project("测试书", "前提", "玄幻", "设定")
                arc = updater.create_arc_plan(project.id, "主线", 1)
                plan = updater.create_chapter_plan(
                    project.id,
                    arc.id,
                    1,
                    "第一章",
                    "开场",
                    ["推进主线"],
                )
                draft = updater.save_draft(
                    chapter_plan_id=plan.id,
                    writer_output=WriterOutput(
                        project_id=project.id,
                        chapter_number=1,
                        title="第一章",
                        body="正文",
                        char_count=2,
                        end_of_chapter_summary="总结",
                    ),
                    raw_response="artifact/meta.json",
                    model_name="fake",
                )
                review_row = ChapterReview(
                    id=new_id(),
                    draft_id=draft.id,
                    verdict="warn",
                    issues_json=json.dumps(
                        [
                            {
                                "rule_name": "hook_soft",
                                "severity": "warning",
                                "description": "钩子偏弱",
                                "entity_names": [],
                                "evidence_refs": ["tail:正文"],
                                "suggested_fix": "补强章末问题",
                            }
                        ],
                        ensure_ascii=False,
                    ),
                    review_meta_json=json.dumps(
                        {
                            "recommended_action": "pause_for_review",
                            "review_summary": "计划奖励 vs 实际奖励",
                            "planned_reward_tags": ["mystery"],
                            "delivered_reward_tags": ["mystery"],
                            "experience_scores": {
                                "narrative_understanding": 0.8,
                                "attentional_focus": 0.7,
                                "emotional_engagement": 0.4,
                                "narrative_presence": 0.6,
                                "payoff_delivery": 0.8,
                                "stall_tolerance": 0.7,
                                "hook_efficiency": 0.4,
                            },
                            "review_notes": ["问题梯子存在，但章末钩子偏软"],
                            "lint_signals": [
                                {
                                    "tool": "vale",
                                    "code": "HookSoft",
                                    "severity": "warning",
                                    "message": "章末力度不足",
                                    "line": 10,
                                    "column": 1,
                                    "evidence_refs": ["line=10"],
                                }
                            ],
                            "evidence_refs": ["tail:正文", "line=10"],
                            "confirmed_signal_refs": ["audience_signal:pacing:arc:节奏"],
                            "reviewer_mode": "llm",
                            "repair_instruction": {
                                "repair_scope": "scene",
                                "failure_type": "hook_failure",
                                "must_fix": ["补强章末钩子"],
                                "must_preserve": ["第一章"],
                                "design_patch": {"hook_type": "hard_cliffhanger"},
                                "evidence_refs": ["tail:正文"],
                            },
                            "repair_verification": {
                                "fixed_all_must_fix": True,
                                "preserved_all_must_preserve": True,
                                "unfixed": [],
                                "broken_preserve_constraints": [],
                                "new_risks": [],
                                "verifier_mode": "rule_only",
                            },
                            "final_gate_decision": {
                                "decision": "force_accept",
                                "forceable": True,
                                "reason": "soft_quality_failure_only",
                                "canon_risk": "low",
                                "residual_issues": ["钩子偏弱"],
                                "requires_human": False,
                            },
                            "repair_exhausted": True,
                            "forced_accept_applied": True,
                            "residual_review_issues": [
                                {
                                    "rule_name": "hook_soft",
                                    "severity": "warning",
                                    "description": "钩子偏弱",
                                    "entity_names": [],
                                    "evidence_refs": ["tail:正文"],
                                    "suggested_fix": "补强章末问题",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
                session.add(review_row)
                plan.acceptance_mode = "force_accept_after_repair"
                plan.repair_attempt_count = 3
                plan.canon_risk_level = "low"
                plan.residual_review_issues_json = json.dumps(
                    [
                        {
                            "rule_name": "hook_soft",
                            "severity": "warning",
                            "description": "钩子偏弱",
                            "entity_names": [],
                            "evidence_refs": ["tail:正文"],
                            "suggested_fix": "补强章末问题",
                        }
                    ],
                    ensure_ascii=False,
                )
                session.flush()
                updater.save_chapter_rewrite_attempt(
                    project_id=project.id,
                    chapter_number=1,
                    attempt_no=1,
                    trigger_review_id=review_row.id,
                    repair_scope="scene",
                    design_patch={"hook_type": "hard_cliffhanger"},
                    source_draft_id=draft.id,
                    result_draft_id=draft.id,
                    result_verdict="warn",
                    result_review_id=review_row.id,
                    verification={
                        "fixed_all_must_fix": True,
                        "preserved_all_must_preserve": True,
                        "unfixed": [],
                        "broken_preserve_constraints": [],
                        "new_risks": [],
                        "verifier_mode": "rule_only",
                    },
                    source_chapter_plan={
                        "chapter_number": 1,
                        "title": "第一章",
                        "transient_overlay": False,
                    },
                    result_chapter_plan={
                        "chapter_number": 1,
                        "title": "第一章",
                        "transient_overlay": True,
                    },
                    source_band_plan={"band_id": "band-1", "transient_overlay": False},
                    result_band_plan={"band_id": "band-1", "transient_overlay": False},
                    forced_accept_applied=True,
                )
                session.commit()

            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            try:
                api_module._engine = engine
                api_module._SessionFactory = session_factory
                payload = api_module.get_chapter_review(project.id, 1).model_dump(mode="json")
                chapter_payload = api_module.get_chapter(project.id, 1).model_dump(mode="json")
                list_payload = [item.model_dump(mode="json") for item in api_module.list_chapters(project.id)]
                project_payload = api_module.get_project(project.id).model_dump(mode="json")
                self.assertEqual(payload["review_notes"], ["问题梯子存在，但章末钩子偏软"])
                self.assertEqual(payload["lint_signals"][0]["tool"], "vale")
                self.assertEqual(payload["evidence_refs"], ["tail:正文", "line=10"])
                self.assertEqual(payload["confirmed_signal_refs"], ["audience_signal:pacing:arc:节奏"])
                self.assertEqual(payload["reviewer_mode"], "llm")
                self.assertEqual(payload["proposed_design_patch"]["hook_type"], "hard_cliffhanger")
                self.assertEqual(payload["acceptance_mode"], "force_accept_after_repair")
                self.assertEqual(payload["repair_attempt_count"], 3)
                self.assertEqual(payload["canon_risk_level"], "low")
                self.assertEqual(payload["latest_repair_scope"], "scene")
                self.assertTrue(payload["repair_exhausted"])
                self.assertTrue(payload["forced_accept_applied"])
                self.assertEqual(payload["repair_verification"]["verifier_mode"], "rule_only")
                self.assertEqual(payload["final_gate_decision"]["decision"], "force_accept")
                self.assertEqual(payload["rewrite_attempts"][0]["repair_scope"], "scene")
                self.assertTrue(payload["rewrite_attempts"][0]["forced_accept_applied"])
                self.assertEqual(chapter_payload["acceptance_mode"], "force_accept_after_repair")
                self.assertEqual(chapter_payload["repair_attempt_count"], 3)
                self.assertEqual(chapter_payload["canon_risk_level"], "low")
                self.assertEqual(chapter_payload["residual_review_issues"][0]["description"], "钩子偏弱")
                self.assertEqual(list_payload[0]["acceptance_mode"], "force_accept_after_repair")
                self.assertEqual(list_payload[0]["latest_repair_scope"], "scene")
                self.assertEqual(list_payload[0]["canon_risk_level"], "low")
                self.assertEqual(project_payload["chapters"][0]["acceptance_mode"], "force_accept_after_repair")
                self.assertEqual(project_payload["chapters"][0]["latest_repair_scope"], "scene")
            finally:
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory
                engine.dispose()

    def test_trope_templates_and_band_experience_override_api(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("band-override-v27")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    chapter_review_form_mode="off",
                )
            )
            with session_factory() as session:
                updater = StateUpdater(session)
                project = updater.create_project("测试书", "前提", "玄幻", "设定")
                arc = updater.create_arc_plan(project.id, "主线", 1)
                updater.create_chapter_plan(project.id, arc.id, 1, "第一章", "开场", ["推进主线"])
                updater.create_chapter_plan(project.id, arc.id, 2, "第二章", "承压", ["加压"])
                session.add(
                    ArcStructureDraft(
                        id=new_id(),
                        project_id=project.id,
                        arc_id=arc.id,
                        phase_layout_json="[]",
                        key_beats_json=json.dumps(["开场遇险", "揭开新线索"], ensure_ascii=False),
                        thread_priorities_json="[]",
                        hotspot_candidates_json="[]",
                        compression_candidates_json="[]",
                        reader_promise_json=json.dumps(
                            {
                                "genre_promise": "玄幻网文",
                                "pleasure_promise": "稳定爽点与悬念",
                                "core_pleasures": ["翻盘", "揭秘"],
                                "ambiguity_mode": "managed",
                                "world_legibility_target": "规则要看得懂",
                            },
                            ensure_ascii=False,
                        ),
                        arc_payoff_map_json=json.dumps(
                            {
                                "macro_payoffs": [],
                                "awe_kit": ["反转"],
                                "revelation_layers": [],
                                "ambiguity_constraints": ["关键翻盘必须回指规则"],
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                updater.save_band_experience_plan(
                    project_id=project.id,
                    arc_id=arc.id,
                    schedule=__import__("forwin.protocol.experience", fromlist=["BandDelightSchedule"]).BandDelightSchedule(
                        band_id="band:1:2",
                        chapter_start=1,
                        chapter_end=2,
                        scheduled_rewards=[
                            __import__("forwin.protocol.experience", fromlist=["BandRewardItem"]).BandRewardItem(
                                chapter_hint=1,
                                category="power",
                                template_id="power-hidden-edge",
                                intent="micro_progress_power",
                            )
                        ],
                        stall_guard_max_gap=1,
                    ),
                )
                session.commit()

            old_engine = api_module._engine
            old_factory = api_module._SessionFactory
            old_orchestrator = api_module._orchestrator
            try:
                api_module._engine = engine
                api_module._SessionFactory = session_factory
                api_module._orchestrator = orchestrator
                categories = {item.category for item in api_module.get_trope_templates()}
                self.assertTrue({"power", "social", "justice", "mystery", "emotion"}.issubset(categories))
                power_templates = api_module.get_trope_templates(category="power")
                self.assertTrue(all(item.category == "power" for item in power_templates))
                summary = api_module.get_trope_template_summary()
                self.assertGreaterEqual(summary.total_count, 5)
                self.assertFalse(summary.is_full_library)
                invalid_validation = api_module.validate_trope_templates(
                    api_module.TropeTemplateValidationRequest(
                        templates=[{"template_id": "only-one", "category": "power"}],
                        require_full=True,
                    )
                )
                self.assertFalse(invalid_validation.ok)
                self.assertIn("full trope library", " ".join(invalid_validation.errors))

                override = api_module.override_band_experience(
                    project.id,
                    "band:1:2",
                    api_module.BandExperienceOverrideRequest.model_validate(
                        {
                            "scheduled_rewards": [
                                {
                                    "chapter_hint": 1,
                                    "category": "social",
                                    "template_id": "social-face-slap",
                                    "intent": "social_dominance",
                                },
                                {
                                    "chapter_hint": 2,
                                    "category": "mystery",
                                    "template_id": "mystery-locked-clue",
                                    "intent": "mystery_clue_or_reveal",
                                },
                            ],
                            "curiosity_beats": [
                                {
                                    "chapter_hint": 1,
                                    "question_open": "谁在背后逼迫主角",
                                    "question_resolve": "确认有人盯上主角",
                                    "escalated_question": "那条规则到底是谁制定的",
                                }
                            ],
                            "immersion_anchor_scene_goal": "第一章必须给出雨夜压迫感",
                        }
                    ),
                )
                self.assertEqual(override.chapter_start, 1)
                with session_factory() as session:
                    band = session.execute(select(BandExperiencePlan).limit(1)).scalar_one()
                    plan_one = session.execute(
                        select(ChapterPlan).where(ChapterPlan.project_id == project.id, ChapterPlan.chapter_number == 1)
                    ).scalar_one()
                band_payload = json.loads(band.schedule_json)
                chapter_payload = json.loads(plan_one.experience_plan_json)
                self.assertEqual(band_payload["scheduled_rewards"][0]["category"], "social")
                self.assertEqual(band_payload["curiosity_beats"][0]["question_open"], "谁在背后逼迫主角")
                self.assertIn("social-face-slap", chapter_payload["selected_template_ids"])
                self.assertEqual(chapter_payload["question_hook"], "谁在背后逼迫主角")
            finally:
                api_module._engine = old_engine
                api_module._SessionFactory = old_factory
                api_module._orchestrator = old_orchestrator
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()


class SkillRuntimeTraceRegressionTests(unittest.TestCase):
    def test_review_and_rewrite_persists_prompt_trace_chain(self) -> None:
        from forwin.models.genesis import PromptTrace
        from forwin.state.repo import StateRepository

        with TemporaryDirectory() as tmpdir:
            db_path = postgres_test_url("skill-trace")
            config = Config(
                database_url=db_path,
                minimax_api_key="test-key",
                minimax_base_url="http://example.invalid",
                minimax_model="fake-model",
                    chapter_review_form_mode="off",
                skill_runtime_enabled=True,
                skill_registry_path="forwin_skills",
            )
            orchestrator = WritingOrchestrator(config)
            try:
                with orchestrator._SessionFactory() as session:
                    repo = StateRepository(session)
                    updater = StateUpdater(session)
                    project = updater.create_project(
                        title="Trace 书",
                        premise="测试 rewrite 链路的 prompt trace。",
                        genre="玄幻",
                        target_total_chapters=3,
                    )
                    arc = updater.create_arc_plan(
                        project_id=project.id,
                        arc_synopsis="第一段冲突",
                        version=1,
                        status="active",
                        arc_number=1,
                        chapter_start=1,
                        chapter_end=3,
                    )
                    chapter_plan = updater.create_chapter_plan(
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=1,
                        title="第一章",
                        one_line="建立危机",
                        goals=["建立危机"],
                    )
                    context = ChapterContextPack(
                        project_id=project.id,
                        project_title=project.title,
                        premise=project.premise,
                        genre=project.genre,
                        setting_summary=project.setting_summary,
                        chapter_number=1,
                        chapter_plan_title="第一章",
                        chapter_plan_one_line="建立危机",
                        chapter_goals=["建立危机"],
                    )
                    initial_output = WriterOutput(
                        project_id=project.id,
                        chapter_number=1,
                        title="第一章",
                        body="初稿正文",
                        char_count=4,
                        end_of_chapter_summary="初稿摘要",
                        generation_meta={
                            "prompt_trace": {
                                "trace_scope": "writer",
                                "stage_key": "chapter_draft",
                                "template_id": "writer:single",
                                "template_version": "v1",
                                "effective_system_prompt": "writer draft",
                                "prompt_layers": [{"role": "system", "content": "writer draft"}],
                                "input_snapshot": {"selected_skills": [{"id": "writer.chapter-outline"}]},
                                "model_profile": {"model": "fake-model"},
                                "attempts": [],
                                "output_summary": {"skill_summary": [{"id": "writer.chapter-outline"}]},
                            }
                        },
                    )
                    rewritten_output = WriterOutput(
                        project_id=project.id,
                        chapter_number=1,
                        title="第一章 重写",
                        body="重写正文",
                        char_count=4,
                        end_of_chapter_summary="重写摘要",
                        generation_meta={
                            "prompt_trace": {
                                "trace_scope": "writer",
                                "stage_key": "chapter_rewrite",
                                "template_id": "writer:single",
                                "template_version": "v1",
                                "effective_system_prompt": "writer rewrite",
                                "prompt_layers": [{"role": "system", "content": "writer rewrite"}],
                                "input_snapshot": {"selected_skills": [{"id": "writer.style-control"}]},
                                "model_profile": {"model": "fake-model"},
                                "attempts": [],
                                "output_summary": {"skill_summary": [{"id": "writer.style-control"}]},
                            }
                        },
                    )
                    first_review = ReviewVerdict(
                        verdict="fail",
                        issues=[
                            ContinuityIssue(
                                rule_name="need_rewrite",
                                severity="error",
                                description="需要重写",
                            )
                        ],
                        repair_instruction=RepairInstruction(
                            repair_scope="scene",
                            failure_type="continuity",
                            must_fix=["修复连续性"],
                            must_preserve=["建立危机"],
                        ),
                        prompt_trace={
                            "trace_scope": "reviewer",
                            "stage_key": "chapter_review",
                            "template_id": "reviewer:chapter_review",
                            "template_version": "v1",
                            "effective_system_prompt": "reviewer fail",
                            "prompt_layers": [{"role": "system", "content": "reviewer fail"}],
                            "input_snapshot": {"selected_skills": [{"id": "reviewer.chapter-continuity"}]},
                            "model_profile": {},
                            "attempts": [],
                            "output_summary": {"skill_summary": [{"id": "reviewer.chapter-continuity"}]},
                        },
                    )
                    second_review = ReviewVerdict(
                        verdict="pass",
                        issues=[],
                        prompt_trace={
                            "trace_scope": "reviewer",
                            "stage_key": "chapter_review",
                            "template_id": "reviewer:chapter_review",
                            "template_version": "v1",
                            "effective_system_prompt": "reviewer pass",
                            "prompt_layers": [{"role": "system", "content": "reviewer pass"}],
                            "input_snapshot": {"selected_skills": [{"id": "reviewer.repair-plan"}]},
                            "model_profile": {},
                            "attempts": [],
                            "output_summary": {"skill_summary": [{"id": "reviewer.repair-plan"}]},
                        },
                    )

                    with patch.object(
                        orchestrator,
                        "_review_current_output",
                        side_effect=[first_review, second_review],
                    ), patch.object(
                        orchestrator,
                        "_write_chapter_with_attention_fallback",
                        return_value=rewritten_output,
                    ), patch.object(
                        orchestrator,
                        "_apply_repair_patch",
                        return_value=({}, context, {}, {}, ""),
                    ):
                        orchestrator.retrieval_broker = SimpleNamespace(
                            build_chapter_context=lambda repo, project_id, chapter_plan: context
                        )
                        output, review, forced = orchestrator._review_and_maybe_rewrite(
                            session=session,
                            repo=repo,
                            updater=updater,
                            checker=SimpleNamespace(),
                            project_id=project.id,
                            chapter_plan=chapter_plan,
                            context=context,
                            writer_output=initial_output,
                        )
                    session.flush()
                    traces = session.execute(
                        select(PromptTrace)
                        .where(PromptTrace.project_id == project.id)
                    ).scalars().all()

                self.assertFalse(forced)
                self.assertEqual(output.title, "第一章 重写")
                self.assertEqual(review.verdict, "pass")
                trace_by_id = {trace.id: trace for trace in traces}
                roots = [trace for trace in traces if not str(trace.parent_trace_id or "").strip()]
                self.assertEqual(len(roots), 1)
                ordered_traces = [roots[0]]
                while True:
                    current = ordered_traces[-1]
                    children = [
                        trace
                        for trace in traces
                        if str(trace.parent_trace_id or "").strip() == current.id
                    ]
                    if not children:
                        break
                    self.assertEqual(len(children), 1)
                    ordered_traces.append(children[0])

                self.assertEqual([trace.trace_scope for trace in ordered_traces], ["writer", "reviewer", "writer", "reviewer"])
                self.assertEqual(ordered_traces[1].parent_trace_id, ordered_traces[0].id)
                self.assertEqual(ordered_traces[2].parent_trace_id, ordered_traces[1].id)
                self.assertEqual(ordered_traces[3].parent_trace_id, ordered_traces[2].id)
                self.assertEqual(set(trace_by_id), {trace.id for trace in ordered_traces})
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()


if __name__ == "__main__":
    unittest.main()
