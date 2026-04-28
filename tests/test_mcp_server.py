from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import TypeAdapter

import forwin.api as api_module
from forwin.api_schemas import BookGenesisPatchRequest, ProjectCreateRequest
from forwin.config import Config
from forwin.mcp.client import ForWinAPIClient
from forwin.mcp.http import build_asgi_app, build_mcp_server
from forwin.mcp.models import (
    ChapterDetailView,
    ChapterSummaryView,
    GenesisView,
    MutationResult,
    ProjectView,
    TaskView,
    WorldModelConflictListView,
    WorldModelConflictView,
    WorldModelExportView,
    WorldModelPageView,
    WorldModelSnapshotView,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import ChapterDraft
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.updater import StateUpdater


class ForWinAPIClientUnitTests(unittest.TestCase):
    def test_invalid_stage_key_is_rejected_before_http_request(self) -> None:
        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(lambda request: self.fail(f"unexpected request: {request.url}")),
        )

        with self.assertRaisesRegex(ValueError, "Unsupported stage_key"):
            asyncio.run(client.genesis_stage_generate(project_id="project-1", stage_key="bad-stage"))  # type: ignore[arg-type]

    def test_http_4xx_becomes_value_error_with_api_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"detail": "active generation task"}, request=request)

        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaisesRegex(ValueError, "active generation task"):
            asyncio.run(client.project_continue_generation(project_id="project-1"))

    def test_http_5xx_becomes_runtime_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "backend unavailable"}, request=request)

        client = ForWinAPIClient(
            base_url="http://forwin.invalid",
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
            asyncio.run(client.project_get("project-1"))


class ForWinMCPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.database_url = postgres_test_url("forwin-mcp")
        engine = get_engine(self.database_url)
        init_db(engine)
        self.engine = engine
        self.session_factory = get_session_factory(engine)

        self.old_session_factory = api_module._SessionFactory
        self.old_config = api_module._config
        self.old_runtime_settings = api_module._runtime_settings
        with api_module._tasks_lock:
            self.old_tasks = dict(api_module._tasks)
            api_module._tasks.clear()

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

        self.api_transport = httpx.ASGITransport(app=api_module.app)
        self.api_client = ForWinAPIClient(base_url="http://testserver", transport=self.api_transport)
        self.mcp = build_mcp_server(api_client=self.api_client)
        self.mcp_app = build_asgi_app(api_client=self.api_client, mcp_server=self.mcp)

    def tearDown(self) -> None:
        api_module._SessionFactory = self.old_session_factory
        api_module._config = self.old_config
        api_module._runtime_settings = self.old_runtime_settings
        with api_module._tasks_lock:
            api_module._tasks.clear()
            api_module._tasks.update(self.old_tasks)
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _call_tool(self, name: str, arguments: dict | None = None, *, raise_on_error: bool = True):
        async def run():
            async with Client(self.mcp) as client:
                return await client.call_tool(name, arguments or {}, raise_on_error=raise_on_error)

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
                    "execution_bootstrap": {"operation_mode": "blackbox", "root_ready": True},
                }
            ),
        )
        for stage_key in ("brief", "world", "map", "story_engine", "book_blueprint", "bootstrap"):
            api_module.lock_project_genesis_stage(created.project_id, stage_key)
        return created.project_id

    def _create_project_with_draft(self) -> tuple[str, int]:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="Chapter MCP Book",
                premise="用来测试 chapter_list / chapter_get。",
                genre="玄幻",
                creation_status="writing",
            )
            arc = updater.create_arc_plan(project_id=project.id, arc_synopsis="测试 arc")
            plan = updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章 雨夜",
                one_line="主角在雨夜撞上禁术代价。",
                goals=["建立危机"],
            )
            session.add(
                ChapterDraft(
                    chapter_plan_id=plan.id,
                    version=1,
                    body_text="雨夜里，他第一次看见那面会说话的镜子。",
                    summary="主角在雨夜得到了危险线索。",
                    char_count=20,
                    llm_model="fake-model",
                    llm_raw_response="{}",
                )
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
                    "task_list",
                    "task_get",
                    "task_active_generation_check",
                    "task_pause",
                    "chapter_list",
                    "chapter_get",
                    "world_model_get",
                    "world_page_get",
                    "world_conflict_list",
                    "world_export_obsidian",
                ]
            ),
        )
        self.assertTrue(all("Use this when" in (tool.description or "") for tool in tools))

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

        def fake_generate_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
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

        def fake_refine_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            return (
                {"value": "旧王朝崩塌后的百年乱局进入第二次重组前夜。"},
                {
                    "effective_system_prompt": "genesis refine world",
                    "prompt_layers": [{"role": "system", "content": "genesis refine world"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fake-model"},
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_generate_call):
            generated = self._load_model(
                MutationResult,
                self._call_tool(
                    "genesis_stage_generate",
                    {"project_id": created.project.id, "stage_key": "world"},
                ),
            )
        self.assertEqual(generated.genesis.pack["world"]["world_bible"]["overview"], "被 MCP 生成的世界观。")

        with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_refine_call):
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
        world_state = {item.stage_key: item for item in locked.genesis.stage_states}["world"]
        self.assertTrue(world_state.locked)
        self.assertEqual(world_state.status, "locked")

    def test_start_writing_continue_conflict_and_pause_via_mcp(self) -> None:
        project_id = self._create_ready_project()

        def fake_launch_arc_call(_service, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
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

        def noop_continue_project(*args, **kwargs):
            return None

        with (
            patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_launch_arc_call),
            patch("forwin.api._run_continue_project_with_config", new=noop_continue_project),
        ):
            started = self._load_model(
                MutationResult,
                self._call_tool("project_start_writing", {"project_id": project_id}),
            )

        self.assertIsNotNone(started.task)
        self.assertEqual(started.project.creation_status, "writing")
        self.assertEqual(started.task.status, "starting")
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

    def test_chapter_list_and_get_via_mcp(self) -> None:
        project_id, chapter_number = self._create_project_with_draft()

        chapter_list_result = self._call_tool("chapter_list", {"project_id": project_id})
        chapters = self._load_list(TypeAdapter(list[ChapterSummaryView]), chapter_list_result)
        self.assertEqual([item.chapter_number for item in chapters], [1])
        self.assertTrue(chapters[0].has_draft)

        chapter_result = self._call_tool(
            "chapter_get",
            {"project_id": project_id, "chapter_number": chapter_number},
        )
        chapter = self._load_model(ChapterDetailView, chapter_result)
        self.assertEqual(chapter.chapter_number, 1)
        self.assertIn("会说话的镜子", chapter.body)
        self.assertEqual(chapter.summary, "主角在雨夜得到了危险线索。")

    def test_world_model_read_tools_and_export_via_mcp(self) -> None:
        project_id = self._create_ready_project()
        vault_root = str(Path(self.tmpdir.name) / "mcp-vault")

        snapshot = self._load_model(
            WorldModelSnapshotView,
            self._call_tool("world_model_get", {"project_id": project_id, "as_of_chapter": 0}),
        )
        self.assertEqual(snapshot.as_of_chapter, 0)
        self.assertIn("旧城", json.dumps(snapshot.snapshot, ensure_ascii=False))

        page = self._load_model(
            WorldModelPageView,
            self._call_tool("world_page_get", {"project_id": project_id, "page_key": "world:index"}),
        )
        self.assertEqual(page.title, "00_Index")
        self.assertIn("Canon Summary", page.markdown)

        conflicts = self._load_model(
            WorldModelConflictListView,
            self._call_tool("world_conflict_list", {"project_id": project_id}),
        )
        self.assertEqual(conflicts.conflicts, [])

        exported = self._load_model(
            WorldModelExportView,
            self._call_tool("world_export_obsidian", {"project_id": project_id, "vault_root": vault_root}),
        )
        self.assertTrue(exported.ok)
        self.assertTrue((Path(vault_root) / "00_Index.md").exists())
