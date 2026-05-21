from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

from forwin.book_genesis import BookGenesisService
from forwin.genesis_workspace.service import GenesisWorkspaceService
from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.governance import DecisionEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.state.updater import StateUpdater


class _NoApiKeyClient:
    api_key = ""
    codex_enabled = False
    profile_id = ""
    profile_name = ""
    model = "fake-model"
    base_url = ""
    last_call_result = None

    def chat(self, *_args, **_kwargs):  # pragma: no cover - fallback path should avoid this
        raise AssertionError("workspace tests should not call the real LLM")


class GenesisWorkspaceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = get_engine(postgres_test_url("genesis-workspace-service"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _service(self) -> BookGenesisService:
        return BookGenesisService(llm_client=_NoApiKeyClient())

    def _project(self, session, *, project_id: str = "proj-genesis-workspace") -> Project:
        project = Project(
            id=project_id,
            title="Workspace 测试书",
            premise="人类先设计 Genesis，再显式交接写作。",
            genre="玄幻",
            target_total_chapters=6,
        )
        session.add(project)
        session.flush()
        return project

    def test_facade_exposes_workspace_service_without_writing_side_effects(self) -> None:
        service = self._service()
        self.assertIsInstance(service.workspace, GenesisWorkspaceService)

        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = self._project(session)
            revision = service.create_initial_revision(session=session, updater=updater, project=project)
            service.patch_pack(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                patch={
                    "world": {
                        "world_bible": {"overview": "只属于 Genesis Workspace 的世界观。"},
                        "map_atlas": {"overview": "旧城与城外荒原。"},
                        "story_engine": {"long_arcs": ["旧术复苏"]},
                    }
                },
                reason="workspace edit",
            )
            current = service.active_revision(session, project)
            assert current is not None
            service.lock_stage(session=session, updater=updater, project=project, revision=current, stage_key="world")
            session.commit()

            project_row = session.get(Project, project.id)
            arc_count = session.execute(select(ArcPlanVersion).where(ArcPlanVersion.project_id == project.id)).scalars().all()
            chapter_count = session.execute(select(ChapterPlan).where(ChapterPlan.project_id == project.id)).scalars().all()

        assert project_row is not None
        self.assertEqual(project_row.creation_status, "creating")
        self.assertEqual(arc_count, [])
        self.assertEqual(chapter_count, [])

    def test_generate_and_refine_use_facade_trace_patch_point(self) -> None:
        service = self._service()
        observed_stage_keys: list[str] = []

        def fake_call(self, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
            observed_stage_keys.append(str(stage_key))
            if stage_key == "brief":
                return (
                    {
                        "title": "Workspace 测试书",
                        "one_line": "主角在旧城接住复苏的禁术债务。",
                        "audience": "网文读者",
                        "core_emotion": "紧张上升",
                        "core_delight": "破局",
                        "promise": "持续升级",
                        "guardrails": ["不自动写章"],
                    },
                    {
                        "effective_system_prompt": "genesis brief",
                        "prompt_layers": [{"role": "system", "content": "genesis brief"}],
                        "input_snapshot": {"stage_key": stage_key},
                        "model_profile": {"model": "fake-model"},
                        "attempts": [{"attempt": 1, "status": "success"}],
                        "output_summary": {"mode": "success"},
                    },
                )
            return (
                {"value": "更明确的受众"},
                {
                    "effective_system_prompt": "genesis refine",
                    "prompt_layers": [{"role": "system", "content": "genesis refine"}],
                    "input_snapshot": {"stage_key": stage_key},
                    "model_profile": {"model": "fake-model"},
                    "attempts": [{"attempt": 1, "status": "success"}],
                    "output_summary": {"mode": "success"},
                },
            )

        with self.session_factory() as session:
            updater = StateUpdater(session)
            project = self._project(session, project_id="proj-genesis-workspace-trace")
            revision = service.create_initial_revision(session=session, updater=updater, project=project)
            with patch("forwin.book_genesis.BookGenesisService._call_json_with_trace", new=fake_call):
                generated, _trace = service.generate_stage(
                    session=session,
                    updater=updater,
                    project=project,
                    revision=revision,
                    stage_key="brief",
                )
                refined, _trace = service.refine_stage(
                    session=session,
                    updater=updater,
                    project=project,
                    revision=generated,
                    stage_key="brief",
                    instruction="把受众改得更明确",
                    target_path="audience",
                )
            session.commit()

            latest = session.get(BookGenesisRevision, project.active_genesis_revision_id)
            traces = session.execute(select(PromptTrace).where(PromptTrace.project_id == project.id)).scalars().all()
            events = session.execute(select(DecisionEvent).where(DecisionEvent.project_id == project.id)).scalars().all()

        assert latest is not None
        pack = json.loads(latest.pack_json)
        self.assertEqual(pack["book_brief"]["audience"], "更明确的受众")
        self.assertEqual(observed_stage_keys, ["brief", "brief:refine_item"])
        self.assertTrue(any(trace.trace_scope == "genesis" for trace in traces))
        self.assertTrue(any(trace.trace_scope == "genesis_refine" for trace in traces))
        self.assertTrue(any(event.event_type == DecisionEventType.GENESIS_STAGE_GENERATED for event in events))
        self.assertTrue(any(event.event_type == DecisionEventType.GENESIS_STAGE_REFINED for event in events))

    def test_top_level_old_world_sections_are_not_promoted_to_world_root(self) -> None:
        service = self._service()
        with self.session_factory() as session:
            project = self._project(session, project_id="proj-genesis-workspace-legacy")
            revision = BookGenesisRevision(
                project_id=project.id,
                revision=1,
                pack_json=json.dumps(
                    {
                        "book_brief": {"title": "Legacy"},
                        "world_bible": {"overview": "旧顶层 world bible"},
                        "map_atlas": {"overview": "旧顶层 map"},
                        "story_engine": {"long_arcs": ["旧顶层 engine"]},
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(revision)
            session.flush()
            project.active_genesis_revision_id = revision.id
            session.add(project)
            session.commit()

            loaded = service.load_pack(revision)

        self.assertNotEqual(loaded["world"]["world_bible"].get("overview"), "旧顶层 world bible")
        self.assertNotEqual(loaded["world"]["map_atlas"].get("overview"), "旧顶层 map")
        self.assertNotEqual(loaded["world"]["story_engine"].get("long_arcs"), ["旧顶层 engine"])
        self.assertNotIn("world_bible", loaded)
