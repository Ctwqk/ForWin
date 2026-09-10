from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

import forwin.mcp.http as mcp_http
from forwin.api_schema import BookGenesisPatchRequest, ProjectCreateRequest
from forwin.audit.events import DecisionEventInfo, DecisionEventType
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.cli import build_parser
from forwin.config import InfrastructureConfig
from forwin.planning.checkpoints import (
    BandCheckpointDetail,
    BandCheckpointIssueInfo,
)
from forwin.mcp.client import ForWinAPIClient
from forwin.mcp.http import (
    _env_api_timeout_seconds,
    build_asgi_app,
    build_mcp_server,
)
from forwin.mcp.models import (
    BandCheckpointView,
    ChapterDetailView,
    ChapterListView,
    GenesisView,
    GateLedgerReportView,
    MutationResult,
    ProjectListView,
    TaskListView,
    TaskView,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


api_module: HttpRuntimeHarness


class ForWinAPIClientUnitTests(unittest.TestCase):
    def test_project_decision_events_pushes_filters_and_limit_to_api(self) -> None:
        observed: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed.update(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "audit-1",
                            "event_type": "generation_audit_checkpoint_reached",
                        }
                    ]
                },
                request=request,
            )

        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(handler),
        )

        result = asyncio.run(
            client.project_decision_events(
                project_id="project-1",
                event_type="generation_audit_checkpoint_reached",
                event_family="runtime_observation",
                limit=7,
            )
        )

        self.assertEqual(
            observed,
            {
                "event_type": "generation_audit_checkpoint_reached",
                "event_family": "runtime_observation",
                "limit": "7",
            },
        )
        self.assertEqual([item.id for item in result.items], ["audit-1"])

    def test_basic_credentials_are_forwarded_without_plaintext_client_repr(
        self,
    ) -> None:
        observed: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["authorization"] = request.headers["authorization"]
            return httpx.Response(200, json=[], request=request)

        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            basic_username="operator",
            basic_password="private",
            transport=httpx.MockTransport(handler),
        )

        asyncio.run(client.project_list())

        expected = httpx.BasicAuth("operator", "private").auth_flow(
            httpx.Request("GET", "http://forwin.invalid/api/projects")
        )
        authenticated_request = next(expected)
        self.assertEqual(
            observed["authorization"],
            authenticated_request.headers["authorization"],
        )
        self.assertNotIn("operator", repr(client))
        self.assertNotIn("private", repr(client))

    def test_partial_basic_credentials_fail_before_transport_use(self) -> None:
        for username, password in (("operator", ""), ("", "private")):
            with self.subTest(username=bool(username), password=bool(password)):
                with self.assertRaisesRegex(ValueError, "must be set together"):
                    ForWinAPIClient(
                        base_url="http://forwin.invalid",
                        basic_username=username,
                        basic_password=password,
                    )

    def test_active_generation_check_rejects_incomplete_coerced_or_inconsistent_payloads(
        self,
    ) -> None:
        malformed_payloads = (
            {},
            {
                "has_active_generation_task": "false",
                "active_task_ids": [],
                "active_count": "0",
                "safe_to_restart": "true",
                "message": "",
            },
            {
                "has_active_generation_task": False,
                "active_task_ids": [1],
                "active_count": 1,
                "safe_to_restart": True,
                "message": "",
            },
            {
                "has_active_generation_task": False,
                "active_task_ids": ["task-1"],
                "active_count": 1,
                "safe_to_restart": True,
                "message": "",
            },
        )

        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                client = ForWinAPIClient(
                    base_url="http://forwin.invalid",
                    transport=httpx.MockTransport(
                        lambda request, payload=payload: httpx.Response(
                            200,
                            json=payload,
                            request=request,
                        )
                    ),
                )

                with self.assertRaises(ValueError):
                    asyncio.run(client.task_active_generation_check())

    def test_active_generation_check_accepts_complete_strict_consistent_payload(
        self,
    ) -> None:
        payload = {
            "has_active_generation_task": True,
            "active_task_ids": ["task-1"],
            "active_count": 1,
            "safe_to_restart": False,
            "message": "generation active",
        }
        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=payload,
                    request=request,
                )
            ),
        )

        result = asyncio.run(client.task_active_generation_check())

        self.assertEqual(result.model_dump(mode="json"), payload)

    @patch.dict(
        "os.environ",
        {
            "FORWIN_API_BASE_URL": "http://api.internal:8899",
            "FORWIN_HTTP_BASIC_USER": "operator",
            "FORWIN_HTTP_BASIC_PASSWORD": "private",
        },
        clear=True,
    )
    @patch("forwin.mcp.http.ForWinAPIClient")
    def test_mcp_default_client_uses_shared_basic_environment(
        self,
        client_factory,
    ) -> None:
        sentinel = object()
        client_factory.return_value = sentinel

        client = mcp_http._default_api_client()

        self.assertIs(client, sentinel)
        client_factory.assert_called_once_with(
            base_url="http://api.internal:8899",
            timeout=900.0,
            basic_username="operator",
            basic_password="private",
        )

    @patch.dict(
        "os.environ",
        {
            "FORWIN_HTTP_BASIC_USER": "operator",
            "FORWIN_HTTP_BASIC_PASSWORD": "private",
        },
        clear=True,
    )
    @patch("forwin.mcp.client.ForWinAPIClient")
    def test_cli_api_client_uses_shared_basic_environment(
        self,
        client_factory,
    ) -> None:
        from forwin.cli import _api_client

        args = build_parser().parse_args([])

        _api_client(args)

        client_factory.assert_called_once_with(
            base_url="http://127.0.0.1:8899",
            timeout=900.0,
            basic_username="operator",
            basic_password="private",
        )

    def test_invalid_stage_key_is_rejected_before_http_request(self) -> None:
        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(
                lambda request: self.fail(f"unexpected request: {request.url}")
            ),
        )

        with self.assertRaisesRegex(ValueError, "Unsupported stage_key"):
            asyncio.run(
                client.genesis_stage_generate(
                    project_id="project-1", stage_key="bad-stage"
                )
            )  # type: ignore[arg-type]

    def test_http_4xx_becomes_value_error_with_api_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409, json={"detail": "active generation task"}, request=request
            )

        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaisesRegex(ValueError, "active generation task"):
            asyncio.run(client.project_continue_generation(project_id="project-1"))

    def test_http_5xx_becomes_runtime_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503, json={"detail": "backend unavailable"}, request=request
            )

        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
            asyncio.run(client.project_get("project-1"))

    def test_default_timeout_covers_long_genesis_operations(self) -> None:
        client = ForWinAPIClient(base_url="http://forwin.invalid")

        self.assertEqual(client.timeout, 900.0)

    @patch.dict("os.environ", {}, clear=True)
    def test_mcp_server_default_timeout_covers_long_genesis_operations(self) -> None:
        self.assertEqual(_env_api_timeout_seconds(), 900.0)

    def test_cli_default_timeout_covers_long_genesis_operations(self) -> None:
        args = build_parser().parse_args([])

        self.assertEqual(args.api_timeout, 900.0)

    def test_project_view_preserves_total_and_materialized_chapter_counts(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/projects/project-1")
            return httpx.Response(
                200,
                json={
                    "id": "project-1",
                    "title": "Thirty Chapter Run",
                    "genre": "悬疑",
                    "premise": "测试长任务章节数状态。",
                    "target_total_chapters": 30,
                    "chapter_count": 12,
                    "generated_chapter_count": 3,
                    "accepted_chapter_count": 2,
                    "needs_review_chapter_count": 1,
                },
                request=request,
            )

        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(handler),
        )

        project = asyncio.run(client.project_get("project-1"))

        self.assertEqual(project.target_total_chapters, 30)
        self.assertEqual(project.materialized_chapter_count, 12)
        self.assertEqual(project.chapter_count, 12)


class ForWinMCPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        global api_module
        self.tmpdir = TemporaryDirectory()
        self.database_url = postgres_test_url("forwin-mcp")
        engine = get_engine(self.database_url)
        init_db(engine)
        self.engine = engine
        self.session_factory = get_session_factory(engine)

        api_module = HttpRuntimeHarness(
            session_factory=self.session_factory,
            config=InfrastructureConfig(
                database_url=self.database_url,
                minimax_api_key="test-key",
                minimax_base_url="http://example.invalid",
                minimax_model="fake-model",
            ),
            engine=self.engine,
        )

        self.api_transport = httpx.ASGITransport(app=api_module.app)
        self.api_client = ForWinAPIClient(
            base_url="http://testserver", transport=self.api_transport
        )
        self.mcp = build_mcp_server(api_client=self.api_client)
        self.mcp_app = build_asgi_app(api_client=self.api_client, mcp_server=self.mcp)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _call_tool(
        self, name: str, arguments: dict | None = None, *, raise_on_error: bool = True
    ):
        async def run():
            async with Client(self.mcp) as client:
                return await client.call_tool(
                    name, arguments or {}, raise_on_error=raise_on_error
                )

        return asyncio.run(run())

    def _list_tools(self):
        async def run():
            async with Client(self.mcp) as client:
                return await client.list_tools()

        return asyncio.run(run())

    @staticmethod
    def _load_model(model_cls, result):
        payload = ForWinMCPIntegrationTests._result_payload(result)
        if isinstance(payload, model_cls):
            return payload
        return model_cls.model_validate(payload)

    @staticmethod
    def _load_list(adapter, result):
        payload = ForWinMCPIntegrationTests._result_payload(result)
        return adapter.validate_python(payload)

    @staticmethod
    def _result_payload(result):
        if result.structured_content is not None:
            return result.structured_content

        payload = result.data
        if payload is not None:
            if hasattr(payload, "root"):
                payload = getattr(payload, "root")
            if hasattr(payload, "model_dump"):
                return payload.model_dump(mode="json")
            return payload

        for content in result.content:
            text = getattr(content, "text", None)
            if text:
                return json.loads(text)
        return None

    def _create_ready_project(self) -> str:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "MCP Ready Project",
                    "premise": "主角必须在旧城的禁术债务中挣出一条路。",
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
                    "execution_bootstrap": {
                        "pipeline": "strict_blackbox",
                        "root_ready": True,
                    },
                }
            ),
        )
        for stage_key in (
            "brief",
            "world",
            "map",
            "story_engine",
            "book_blueprint",
            "bootstrap",
        ):
            api_module.lock_project_genesis_stage(created.project_id, stage_key)
        return created.project_id

    def _create_project_with_draft(self) -> tuple[str, int]:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="Chapter MCP Book",
                premise="用来测试 chapter_list / chapter_get。",
                genre="玄幻",
                runtime_policy=RuntimePolicy.for_profile("standard"),
                creation_status="writing",
            )
            arc = updater.create_arc_plan(
                project_id=project.id, arc_synopsis="测试 arc"
            )
            plan = updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章 雨夜",
                one_line="主角在雨夜撞上禁术代价。",
                goals=["建立危机"],
            )
            draft = ChapterDraft(
                chapter_plan_id=plan.id,
                version=1,
                body_text="雨夜里，他第一次看见那面会说话的镜子。",
                summary="主角在雨夜得到了危险线索。",
                char_count=20,
                llm_model="fake-model",
                llm_raw_response="{}",
            )
            session.add(draft)
            session.flush()
            session.add(
                ChapterReview(draft_id=draft.id, verdict="warn", issues_json="[]")
            )
            session.commit()
            return project.id, 1

    def test_tool_catalog_matches_phase1_surface(self) -> None:
        tools = self._list_tools()
        names = sorted(tool.name for tool in tools)
        self.assertEqual(
            names,
            sorted(
                [
                    "project_list",
                    "project_get",
                    "project_create",
                    "genesis_get",
                    "genesis_stage_generate",
                    "genesis_stage_refine",
                    "genesis_stage_lock",
                    "project_start_writing",
                    "project_continue_generation",
                    "project_set_gate_delegate",
                    "project_decision_events",
                    "gate_ledger_report",
                    "cost_report",
                    "rule_provenance_report",
                    "project_extend_generation",
                    "task_list",
                    "task_get",
                    "task_active_generation_check",
                    "task_pause",
                    "chapter_list",
                    "chapter_get",
                    "chapter_review_approve",
                    "chapter_review_retry",
                    "band_checkpoint_get",
                    "band_checkpoint_approve",
                    "world_model_get",
                    "world_page_get",
                    "world_conflict_list",
                    "world_export_obsidian",
                ]
            ),
        )
        self.assertTrue(
            all("Use this when" in (tool.description or "") for tool in tools)
        )

    def test_project_decision_events_filters_before_database_limit(self) -> None:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="Decision Event MCP Book",
                premise="验证长任务中的稀疏审计事件仍可查询。",
                genre="悬疑",
                runtime_policy=RuntimePolicy.for_profile("standard"),
                creation_status="writing",
            )
            target = updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project.id,
                    chapter_number=6,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
                    summary="generation audit checkpoint",
                )
            )
            target.created_at = datetime(2000, 1, 1)
            for index in range(205):
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project.id,
                        chapter_number=7,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.STAGE_ENTERED,
                        summary=f"filler event {index}",
                    )
                )
            session.commit()
            project_id = project.id

        payload = self._result_payload(
            self._call_tool(
                "project_decision_events",
                {
                    "project_id": project_id,
                    "event_type": DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
                    "limit": 10,
                },
            )
        )

        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(
            payload["items"][0]["event_type"],
            DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
        )

    def test_gate_ledger_report_via_mcp_returns_json_and_markdown(self) -> None:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="Gate Ledger MCP Book",
                premise="用来验证 gate ledger MCP 报告。",
                genre="悬疑",
                runtime_policy=RuntimePolicy.for_profile("standard"),
                creation_status="writing",
            )
            updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project.id,
                    chapter_number=1,
                    scope="chapter",
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                    actor_type="system",
                    payload=attach_gate_outcome(
                        {},
                        GateOutcome(
                            gate_id="hard_floor",
                            responsibility_domain="draft_quality",
                            scope="chapter",
                            candidate_id="mcp-candidate-1",
                            chapter_number=1,
                            decision="pass",
                        ),
                    ),
                )
            )
            session.commit()
            project_id = project.id

        report_result = self._call_tool(
            "gate_ledger_report",
            {"scope": "project", "project_id": project_id, "format": "json"},
        )
        report_payload = self._result_payload(report_result)
        report = GateLedgerReportView.model_validate(report_payload["result"])
        hard_floor = next(
            item for item in report.metrics if item.gate_id == "hard_floor"
        )
        self.assertEqual(hard_floor.opportunities, 1)
        self.assertEqual(hard_floor.evaluations, 1)

        markdown_result = self._call_tool(
            "gate_ledger_report",
            {"scope": "project", "project_id": project_id, "format": "markdown"},
        )
        markdown_payload = self._result_payload(markdown_result)
        markdown = str(markdown_payload["result"])
        self.assertIn("# Gate Ledger", markdown)
        self.assertIn("post_pass_incident_proxy", markdown)

        default_payload = self._result_payload(
            self._call_tool("gate_ledger_report")
        )
        default_report = GateLedgerReportView.model_validate(
            default_payload["result"]
        )
        self.assertEqual(default_report.scope, "cross_project")

    def test_cost_report_via_mcp_returns_json_and_markdown(self) -> None:
        report_payload = self._result_payload(self._call_tool("cost_report"))
        report = report_payload["result"]

        self.assertEqual(report["totals"]["attempts"], 0)
        self.assertEqual(report["manual_action_count"], 0)

        markdown_payload = self._result_payload(
            self._call_tool("cost_report", {"format": "markdown"})
        )
        self.assertIn("# Cost And Intervention Ledger", markdown_payload["result"])

    def test_rule_provenance_report_via_mcp_returns_json_and_markdown(self) -> None:
        report_payload = self._result_payload(
            self._call_tool("rule_provenance_report")
        )
        report = report_payload["result"]

        self.assertGreater(len(report["global_code_backed_rules"]), 0)
        self.assertIn("project_rules", report)
        self.assertIn("recommendations", report)

        markdown_payload = self._result_payload(
            self._call_tool("rule_provenance_report", {"format": "markdown"})
        )
        self.assertIn("# Rule Provenance Report", markdown_payload["result"])

    def test_project_set_gate_delegate_via_mcp_updates_runtime_policy(self) -> None:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="MCP Reckless Mode",
                premise="测试 MCP 鲁莽模式开关。",
                genre="悬疑",
                target_total_chapters=12,
                runtime_policy=RuntimePolicy.for_profile("standard"),
                creation_status="writing",
            )
            session.commit()
            project_id = project.id

        delegated = self._load_model(
            MutationResult,
            self._call_tool(
                "project_set_gate_delegate",
                {
                    "project_id": project_id,
                    "delegate": "spark",
                    "reason": "delegate review gates to Spark",
                },
            ),
        )

        self.assertIsNotNone(delegated.project)
        self.assertEqual(delegated.project.gate_delegate, "spark")
        restored = self._load_model(
            MutationResult,
            self._call_tool(
                "project_set_gate_delegate",
                {
                    "project_id": project_id,
                    "delegate": "human",
                    "reason": "return review gates to human",
                },
            ),
        )
        self.assertEqual(restored.project.gate_delegate, "human")

    def test_list_tools_return_object_wrappers_for_remote_mcp_clients(self) -> None:
        project_id, _chapter_number = self._create_project_with_draft()

        projects = self._load_model(ProjectListView, self._call_tool("project_list"))
        self.assertTrue(any(project.id == project_id for project in projects.projects))

        tasks = self._load_model(
            TaskListView, self._call_tool("task_list", {"limit": 5})
        )
        self.assertIsInstance(tasks.tasks, list)

        chapters = self._load_model(
            ChapterListView, self._call_tool("chapter_list", {"project_id": project_id})
        )
        self.assertEqual([item.chapter_number for item in chapters.chapters], [1])

    def test_extend_generation_via_mcp_appends_future_plans(self) -> None:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="MCP Extend Book",
                premise="测试追加续写计划。",
                genre="悬疑",
                target_total_chapters=2,
                runtime_policy=RuntimePolicy.for_profile("standard"),
                creation_status="writing",
            )
            arc = updater.create_arc_plan(
                project_id=project.id,
                arc_synopsis="已完成弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=2,
                planned_target_size=2,
            )
            for number in (1, 2):
                plan = updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=number,
                    title=f"第{number}章",
                    one_line="已完成",
                    goals=["已完成"],
                )
                plan.status = "accepted"
            session.commit()
            project_id = project.id

        result = self._load_model(
            MutationResult,
            self._call_tool(
                "project_extend_generation",
                {
                    "project_id": project_id,
                    "additional_chapters": 2,
                    "continuity_guard": "最新 canon 剩余79分钟，不要回退成几天。",
                    "reason": "mcp regression",
                },
            ),
        )

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.project)
        self.assertEqual(result.project.chapter_count, 4)
        self.assertEqual(result.project.generation_control.planned_chapters, [3, 4])
        with self.session_factory() as session:
            plans = (
                session.query(ChapterPlan)
                .filter(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number >= 3,
                )
                .order_by(ChapterPlan.chapter_number)
                .all()
            )
            arc = (
                session.query(ArcPlanVersion)
                .filter(
                    ArcPlanVersion.project_id == project_id,
                    ArcPlanVersion.arc_number == 2,
                )
                .one()
            )

        self.assertEqual(arc.status, "planned")
        self.assertEqual([plan.status for plan in plans], ["planned", "planned"])
        self.assertIn("79分钟", plans[0].goals_json)

    def test_task_get_includes_task_timestamps(self) -> None:
        task_id = "task-with-time"
        task = api_module._create_task_record(
            "timestamp regression",
            title="Timestamp Regression",
            subtitle="MCP task_get",
            requested_chapters=1,
        )
        api_module._persist_generation_task(task_id, task)

        result = self._load_model(
            TaskView, self._call_tool("task_get", {"task_id": task_id})
        )

        self.assertTrue(result.created_at)
        self.assertTrue(result.updated_at)

    def test_task_get_includes_generation_run_target(self) -> None:
        task_id = "task-with-run-target"
        task = api_module._create_task_record(
            "run target regression",
            title="Run Target Regression",
            subtitle="MCP task_get",
            requested_chapters=11,
        )
        task["run_until_chapter"] = 60
        api_module._persist_generation_task(task_id, task)

        result = self._load_model(
            TaskView, self._call_tool("task_get", {"task_id": task_id})
        )

        self.assertEqual(result.requested_chapters, 11)
        self.assertEqual(result.run_until_chapter, 60)

    def test_task_get_includes_generation_lease_diagnostics(self) -> None:
        task_id = "task-with-lease"
        task = api_module._create_task_record(
            "lease regression",
            title="Lease Regression",
            subtitle="MCP task_get",
            requested_chapters=1,
        )
        task["lease_owner"] = "worker-1"
        task["lease_expires_at"] = api_module._utcnow()
        task["heartbeat_at"] = api_module._utcnow()
        api_module._persist_generation_task(task_id, task)

        result = self._load_model(
            TaskView, self._call_tool("task_get", {"task_id": task_id})
        )

        self.assertEqual(result.lease_owner, "worker-1")
        self.assertTrue(result.lease_expires_at)
        self.assertTrue(result.heartbeat_at)

    def test_health_endpoint_reports_upstream_ok(self) -> None:
        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.mcp_app),
                base_url="http://testserver",
            ) as client:
                return await client.get("/health")

        response = asyncio.run(run())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["upstream"], "ok")

    def test_project_create_and_genesis_get_via_mcp(self) -> None:
        created_result = self._call_tool(
            "project_create",
            {
                "title": "MCP Genesis Book",
                "premise": "主角在雨夜得到一枚会说话的镜子。",
                "genre": "玄幻",
                "target_total_chapters": 12,
            },
        )
        created = self._load_model(MutationResult, created_result)
        self.assertIsNotNone(created.project)
        self.assertEqual(created.project.creation_status, "creating")
        self.assertFalse(created.project.can_start_writing)
        self.assertTrue(created.workspace_url)

        genesis_result = self._call_tool(
            "genesis_get",
            {"project_id": created.project.id},
        )
        genesis = self._load_model(GenesisView, genesis_result)
        self.assertEqual(genesis.project_id, created.project.id)
        self.assertFalse(genesis.can_start_writing)
        self.assertTrue(any(item.stage_key == "brief" for item in genesis.stage_states))

    def test_genesis_generate_refine_and_lock_via_mcp(self) -> None:
        created = self._load_model(
            MutationResult,
            self._call_tool(
                "project_create",
                {
                    "title": "MCP Genesis Flow",
                    "premise": "先生成，再细化，再锁定。",
                    "genre": "玄幻",
                },
            ),
        )
        assert created.project is not None

        def fake_generate_call(
            _service,
            *,
            messages,
            fallback,
            stage_key,
            temperature=0.45,
            max_tokens=None,
        ):
            return (
                {
                    "overview": "被 MCP 生成的世界观。",
                    "axioms": ["规则 1"],
                    "history_slice": "旧王朝崩塌后。",
                    "naming_style": "简洁",
                    "forbidden_zones": [],
                },
                {
                    "effective_system_prompt": "genesis world",
                    "prompt_layers": [{"role": "system", "content": "genesis world"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fake-model"},
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        def fake_refine_call(
            _service,
            *,
            messages,
            fallback,
            stage_key,
            temperature=0.45,
            max_tokens=None,
        ):
            return (
                {"value": "旧王朝崩塌后的百年乱局进入第二次重组前夜。"},
                {
                    "effective_system_prompt": "genesis refine world",
                    "prompt_layers": [
                        {"role": "system", "content": "genesis refine world"}
                    ],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fake-model"},
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        with patch(
            "forwin.genesis.BookGenesisService._call_json_with_trace",
            new=fake_generate_call,
        ):
            generated = self._load_model(
                MutationResult,
                self._call_tool(
                    "genesis_stage_generate",
                    {"project_id": created.project.id, "stage_key": "world"},
                ),
            )
        self.assertEqual(
            generated.genesis.pack["world"]["world_bible"]["overview"],
            "被 MCP 生成的世界观。",
        )

        with patch(
            "forwin.genesis.BookGenesisService._call_json_with_trace",
            new=fake_refine_call,
        ):
            refined = self._load_model(
                MutationResult,
                self._call_tool(
                    "genesis_stage_refine",
                    {
                        "project_id": created.project.id,
                        "stage_key": "world",
                        "instruction": "把历史切片改得更像旧王朝崩塌后的第二次重组前夜。",
                        "target_path": "history_slice",
                    },
                ),
            )
        self.assertEqual(
            refined.genesis.pack["world"]["world_bible"]["history_slice"],
            "旧王朝崩塌后的百年乱局进入第二次重组前夜。",
        )

        locked = self._load_model(
            MutationResult,
            self._call_tool(
                "genesis_stage_lock",
                {"project_id": created.project.id, "stage_key": "world"},
            ),
        )
        world_state = {item.stage_key: item for item in locked.genesis.stage_states}[
            "world"
        ]
        self.assertTrue(world_state.locked)
        self.assertEqual(world_state.status, "locked")

    def test_start_writing_continue_conflict_and_pause_via_mcp(self) -> None:
        project_id = self._create_ready_project()

        def fake_launch_arc_call(
            _service,
            *,
            messages,
            fallback,
            stage_key,
            temperature=0.45,
            max_tokens=None,
        ):
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
                        "prompt_layers": [
                            {"role": "system", "content": "launch arc planner"}
                        ],
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

        with patch(
            "forwin.genesis.BookGenesisService._call_json_with_trace",
            new=fake_launch_arc_call,
        ):
            started = self._load_model(
                MutationResult,
                self._call_tool("project_start_writing", {"project_id": project_id, "long_run_mode": "soak_test", "isolated": True}),
            )

        self.assertIsNotNone(started.task)
        self.assertEqual(started.project.creation_status, "writing")
        self.assertEqual(started.project.target_total_chapters, 6)
        self.assertEqual(started.project.materialized_chapter_count, 3)
        self.assertEqual(started.project.chapter_count, 3)
        self.assertEqual(started.task.status, "queued")
        self.assertEqual(started.task.current_stage, "queued")

        active_check = self._call_tool(
            "task_active_generation_check",
            {"project_id": project_id},
        )
        active_payload = self._result_payload(active_check)
        self.assertTrue(active_payload["has_active_generation_task"])
        self.assertEqual(active_payload["active_count"], 1)

        with self.assertRaises(ToolError):
            self._call_tool(
                "project_continue_generation",
                {"project_id": project_id},
            )

        paused = self._load_model(
            MutationResult,
            self._call_tool("task_pause", {"task_id": started.task.task_id}),
        )
        self.assertIsNotNone(paused.task)
        self.assertTrue(paused.task.pause_requested)
        self.assertIn("安全暂停", paused.message)

    def test_continue_generation_via_mcp_exposes_public_task_state(self) -> None:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="MCP Auto Continue Book",
                premise="测试 MCP 续跑参数透传。",
                genre="玄幻",
                target_total_chapters=60,
                runtime_policy=RuntimePolicy.for_profile("standard"),
                creation_status="writing",
            )
            arc = updater.create_arc_plan(
                project_id=project.id,
                arc_synopsis="第一段自动续跑弧线",
                status="active",
                arc_number=1,
                chapter_start=1,
                chapter_end=12,
                planned_target_size=12,
            )
            for chapter_number in range(1, 13):
                updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=chapter_number,
                    title=f"第{chapter_number}章",
                    one_line="计划章节",
                    goals=["推进主线"],
                )
            session.commit()
            project_id = project.id

        result = self._load_model(
            MutationResult,
            self._call_tool(
                "project_continue_generation",
                {
                    "project_id": project_id,
                    "run_until_chapter": 12,
                    "long_run_mode": "soak_test",
                    "isolated": True,
                    "auto_continue": True,
                },
            ),
        )

        self.assertIsNotNone(result.task)
        self.assertEqual(result.task.long_run_mode, "soak_test")
        self.assertTrue(result.task.isolated)
        self.assertEqual(result.task.project_id, project_id)
        self.assertEqual(result.task.status, "queued")
        self.assertEqual(result.task.current_stage, "queued")
        self.assertEqual(result.task.requested_chapters, 12)
        self.assertEqual(result.task.run_until_chapter, 12)

        fetched = self._load_model(
            TaskView,
            self._call_tool("task_get", {"task_id": result.task.task_id}),
        )
        self.assertEqual(fetched.task_id, result.task.task_id)
        self.assertEqual(fetched.requested_chapters, 12)
        self.assertEqual(fetched.run_until_chapter, 12)

        active = self._result_payload(
            self._call_tool(
                "task_active_generation_check",
                {"project_id": project_id},
            )
        )
        self.assertTrue(active["has_active_generation_task"])
        self.assertEqual(active["active_task_ids"], [result.task.task_id])

    def test_chapter_list_and_get_via_mcp(self) -> None:
        project_id, chapter_number = self._create_project_with_draft()

        chapter_list_result = self._call_tool(
            "chapter_list", {"project_id": project_id}
        )
        chapters = self._load_model(ChapterListView, chapter_list_result).chapters
        self.assertEqual([item.chapter_number for item in chapters], [1])
        self.assertTrue(chapters[0].has_draft)
        self.assertTrue(chapters[0].has_review)

        chapter_result = self._call_tool(
            "chapter_get",
            {"project_id": project_id, "chapter_number": chapter_number},
        )
        chapter = self._load_model(ChapterDetailView, chapter_result)
        self.assertEqual(chapter.chapter_number, 1)
        self.assertTrue(chapter.has_draft)
        self.assertTrue(chapter.has_review)
        self.assertIn("会说话的镜子", chapter.body)
        self.assertEqual(chapter.summary, "主角在雨夜得到了危险线索。")

    def test_chapter_review_approve_via_mcp(self) -> None:
        project_id, chapter_number = self._create_project_with_draft()
        with self.session_factory() as session:
            plan = (
                session.query(ChapterPlan)
                .filter_by(project_id=project_id, chapter_number=chapter_number)
                .one()
            )
            plan.status = "drafted"
            session.commit()

        accepted_calls = []

        def accept_review(project_id_arg, chapter_number_arg, *, reason=""):
            args = (project_id_arg, chapter_number_arg)
            kwargs = {"reason": reason}
            accepted_calls.append((args, kwargs))
            return {
                "status": "accepted",
                "message": "第1章已接受并写入 canon。",
                "frozen_artifact": "artifact.json",
            }

        old_pipeline = api_module._pipeline
        api_module._pipeline = type(
            "FakePipeline", (), {"accept_review": staticmethod(accept_review)}
        )()
        try:
            result = self._call_tool(
                "chapter_review_approve",
                {
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "reason": "MCP operator accepted clean review",
                },
            )
        finally:
            api_module._pipeline = old_pipeline

        payload = self._result_payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["frozen_artifact"], "artifact.json")
        self.assertEqual(accepted_calls[0][0], (project_id, chapter_number))
        self.assertEqual(
            accepted_calls[0][1]["reason"], "MCP operator accepted clean review"
        )

    def test_chapter_review_retry_via_mcp(self) -> None:
        project_id, chapter_number = self._create_project_with_draft()
        with self.session_factory() as session:
            plan = (
                session.query(ChapterPlan)
                .filter_by(project_id=project_id, chapter_number=chapter_number)
                .one()
            )
            plan.status = "needs_review"
            plan.residual_review_issues_json = '[{"rule_name":"stale_error"}]'
            plan.canon_risk_level = "high"
            session.commit()

        result = self._call_tool(
            "chapter_review_retry",
            {
                "project_id": project_id,
                "chapter_number": chapter_number,
                "reason": "MCP operator retries stale review gate",
            },
        )

        payload = self._result_payload(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "planned")
        with self.session_factory() as session:
            plan = (
                session.query(ChapterPlan)
                .filter_by(project_id=project_id, chapter_number=chapter_number)
                .one()
            )
            self.assertEqual(plan.status, "planned")
            self.assertEqual(plan.residual_review_issues_json, "[]")
            self.assertEqual(plan.canon_risk_level, "")

    def test_band_checkpoint_get_and_approve_via_mcp(self) -> None:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="Checkpoint MCP Book",
                premise="用来测试 band checkpoint MCP 工具。",
                genre="玄幻",
                runtime_policy=RuntimePolicy.for_profile("standard"),
                creation_status="writing",
            )
            arc = updater.create_arc_plan(
                project_id=project.id, arc_synopsis="checkpoint arc"
            )
            checkpoint = updater.save_band_checkpoint(
                BandCheckpointDetail(
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id="band-1",
                    chapter_start=1,
                    chapter_end=1,
                    trigger_source="auto_band_end",
                    boundary_kind="band_end",
                    boundary_chapter=1,
                    status="warn",
                    summary="band checkpoint 需要人工处理。",
                    issues=[
                        BandCheckpointIssueInfo(
                            code="next_band_compatibility",
                            severity="warning",
                            description="下一 band 前提存在风险。",
                        )
                    ],
                )
            )
            session.commit()
            checkpoint_id = checkpoint.id
            project_id = project.id

        fetched = self._load_model(
            BandCheckpointView,
            self._call_tool(
                "band_checkpoint_get", {"project_id": project_id, "band_id": "band-1"}
            ),
        )
        self.assertEqual(fetched.id, checkpoint_id)
        self.assertEqual(fetched.status, "warn")
        self.assertEqual(fetched.issues[0]["code"], "next_band_compatibility")

        approved = self._load_model(
            BandCheckpointView,
            self._call_tool(
                "band_checkpoint_approve",
                {
                    "project_id": project_id,
                    "band_id": "band-1",
                    "status": "overridden",
                    "reason": "MCP operator inspected checkpoint warning and proceeds with stress test.",
                },
            ),
        )
        self.assertEqual(approved.id, checkpoint_id)
        self.assertEqual(approved.status, "overridden")
        self.assertIn("stress test", approved.reason)
