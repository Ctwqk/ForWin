from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from forwin.audit.events import DecisionEventType
from forwin.book_state import BookStateRepository
from forwin.genesis import BookGenesisService
from forwin.genesis.handoff.chapter_materializer import _arc_payload_from_row
from forwin.genesis.handoff.commands import HandoffStageError, StartWritingCommand
from forwin.genesis.handoff.map_bootstrap import (
    GenesisMapBootstrap,
    GenesisMapBootstrapError,
)
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter
from forwin.map.models import MapNodeRow
from forwin.map.protocol import BookMapGenerationResult, MapValidationReport
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.book_state import GraphDeltaRow
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.state.updater import StateUpdater
from tests.helpers.genesis_seed import (
    seeded_map_atlas,
    seeded_story_engine,
    seeded_world_seed,
)
from tests.postgres import postgres_test_url


class _NoApiKeyClient:
    api_key = ""
    codex_enabled = False
    profile_id = ""
    profile_name = ""
    model = "fake-model"
    base_url = ""
    last_call_result = None

    def chat(
        self, *_args, **_kwargs
    ):  # pragma: no cover - tests patch the facade call point
        raise AssertionError("handoff tests should not call the real LLM")


class GenesisHandoffServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = get_engine(postgres_test_url("genesis-handoff-service"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _service(self) -> BookGenesisService:
        return BookGenesisService(llm_client=_NoApiKeyClient())

    def _create_ready_project(
        self, session, service: BookGenesisService, *, project_id: str
    ) -> Project:
        updater = StateUpdater(session)
        project = Project(
            id=project_id,
            title="Handoff 测试书",
            premise="Genesis 完成后由 manual_ui 显式交接写作。",
            genre="玄幻",
            target_total_chapters=6,
        )
        session.add(project)
        session.flush()
        revision = service.create_initial_revision(
            session=session, updater=updater, project=project
        )
        revision = service.patch_pack(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            patch={
                "world": {
                    "world_bible": {"overview": "旧城与禁术并存的世界。"},
                    "map_atlas": seeded_map_atlas("旧城、城外荒原、地下遗迹。"),
                    "story_engine": seeded_story_engine(["旧术复苏"]),
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
                "world_seed": seeded_world_seed(),
            },
        )
        for stage_key in (
            "brief",
            "world",
            "map",
            "story_engine",
            "book_blueprint",
            "world_seed",
        ):
            revision = service.lock_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=stage_key,
            )
        session.commit()
        return project

    def _fake_launch_arc_call(
        self, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None
    ):
        if str(stage_key).startswith("launch_arc_"):
            return (
                {
                    "chapters": [
                        {
                            "title": "雨夜",
                            "one_line": "主角撞上禁术代价。",
                            "goals": ["建立危机"],
                        },
                        {
                            "title": "债务",
                            "one_line": "旧城势力围拢。",
                            "goals": ["扩大冲突"],
                        },
                        {
                            "title": "遗迹",
                            "one_line": "得到下一阶段坐标。",
                            "goals": ["转入下一 arc"],
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

    def test_start_writing_handoff_requires_manual_ui_and_genesis_ready(self) -> None:
        service = self._service()
        with self.session_factory() as session:
            project = Project(
                id="proj-handoff-guards",
                title="Guard",
                premise="Guard",
                genre="玄幻",
                creation_status="creating",
            )
            session.add(project)
            session.commit()
            updater = StateUpdater(session)

            with self.assertRaises(ValueError):
                service.handoff.start_writing(
                    session=session,
                    updater=updater,
                    command=StartWritingCommand(
                        project_id=project.id, actor_type="system"
                    ),
                )

            with self.assertRaises(ValueError):
                service.handoff.start_writing(
                    session=session,
                    updater=updater,
                    command=StartWritingCommand(
                        project_id=project.id, actor_type="manual_ui"
                    ),
                )

    def test_start_writing_materializes_current_arc_without_creating_generation_task(
        self,
    ) -> None:
        service = self._service()
        with self.session_factory() as session:
            project = self._create_ready_project(
                session, service, project_id="proj-handoff-success"
            )
            updater = StateUpdater(session)
            with patch(
                "forwin.genesis.BookGenesisService._call_json_with_trace",
                new=self._fake_launch_arc_call,
            ):
                result = service.handoff.start_writing(
                    session=session,
                    updater=updater,
                    command=StartWritingCommand(
                        project_id=project.id, actor_type="manual_ui"
                    ),
                )
            session.commit()

            project_row = session.get(Project, project.id)
            arcs = (
                session.execute(
                    select(ArcPlanVersion)
                    .where(ArcPlanVersion.project_id == project.id)
                    .order_by(ArcPlanVersion.arc_number.asc())
                )
                .scalars()
                .all()
            )
            chapters = (
                session.execute(
                    select(ChapterPlan)
                    .where(ChapterPlan.project_id == project.id)
                    .order_by(ChapterPlan.chapter_number.asc())
                )
                .scalars()
                .all()
            )
            tasks = (
                session.execute(
                    select(GenerationTask).where(
                        GenerationTask.project_id == project.id
                    )
                )
                .scalars()
                .all()
            )
            events = (
                session.execute(
                    select(DecisionEvent).where(DecisionEvent.project_id == project.id)
                )
                .scalars()
                .all()
            )

        assert project_row is not None
        self.assertEqual(project_row.creation_status, "writing")
        self.assertEqual(project_row.setting_summary, "旧城与禁术并存的世界。")
        self.assertEqual(result.active_arc_number, 1)
        self.assertEqual(result.active_chapter_plan_count, 3)
        self.assertEqual(len(arcs), 2)
        self.assertEqual(arcs[0].status, "active")
        self.assertEqual(arcs[0].planned_target_size, 3)
        self.assertEqual(arcs[1].status, "planned")
        self.assertEqual([chapter.chapter_number for chapter in chapters], [1, 2, 3])
        self.assertEqual(tasks, [])
        self.assertTrue(
            any(
                event.event_type == DecisionEventType.START_WRITING_REQUESTED
                and event.actor_type == "manual_ui"
                for event in events
            )
        )
        start_event = next(
            event
            for event in events
            if event.event_type == DecisionEventType.START_WRITING_REQUESTED
        )
        handoff = json.loads(start_event.payload_json)["rule_provenance_handoff"]
        self.assertIn(
            "reference.language_generic_exact",
            handoff["global_code_backed_rules"],
        )
        self.assertEqual(handoff["project_active_rules"], [])
        self.assertIn("external_project_rules", handoff)
        self.assertTrue(
            any(
                event.event_type == DecisionEventType.MAP_GENERATION_SUCCEEDED
                for event in events
            )
        )

    def _start_writing(self, service: BookGenesisService, session, project_id: str):
        return service.handoff.start_writing(
            session=session,
            updater=StateUpdater(session),
            command=StartWritingCommand(project_id=project_id, actor_type="manual_ui"),
        )

    def test_start_writing_commits_world_seed_after_map_and_before_chapter_plans(
        self,
    ) -> None:
        service = self._service()
        observed: dict[str, object] = {}
        with self.session_factory() as session:
            project = self._create_ready_project(
                session, service, project_id="proj-handoff-seed-order"
            )
            fake_launch_arc_call = self._fake_launch_arc_call

            def recording_launch_arc_call(_owner, **kwargs):
                if str(kwargs["stage_key"]).startswith("launch_arc_"):
                    observed["character_names"] = sorted(
                        node.name
                        for node in BookStateRepository(session).list_world_nodes(
                            project.id
                        )
                        if str(node.node_type) == "character"
                    )
                    observed["map_node_count"] = session.execute(
                        select(func.count(MapNodeRow.id)).where(
                            MapNodeRow.project_id == project.id
                        )
                    ).scalar_one()
                return fake_launch_arc_call(**kwargs)

            with patch(
                "forwin.genesis.BookGenesisService._call_json_with_trace",
                new=recording_launch_arc_call,
            ):
                result = self._start_writing(service, session, project.id)
            session.commit()
            revision = session.get(
                BookGenesisRevision, project.active_genesis_revision_id
            )
            revision_status = revision.status if revision is not None else ""

        self.assertEqual(observed["character_names"], ["陶然", "顾衡"])
        self.assertGreater(observed["map_node_count"], 0)
        self.assertIs(result.world_seed_summary["skipped"], False)
        self.assertEqual(
            result.world_seed_summary["actor_count"],
            len(seeded_world_seed()["actors"]),
        )
        self.assertEqual(revision_status, "locked")
        self.assertEqual(result.project_status, "writing")

    def test_map_failure_is_typed_and_persists_failure_event(self) -> None:
        service = self._service()
        with self.session_factory() as session:
            project = self._create_ready_project(
                session, service, project_id="proj-handoff-map-failure"
            )
            # The failure text names no map at all: the handoff must go by type.
            invalid_map = BookMapGenerationResult(
                project_id=project.id,
                validation_report=MapValidationReport(
                    valid=False, errors=["anchor node unreachable"]
                ),
            )
            with (
                patch(
                    "forwin.genesis.BookGenesisService._call_json_with_trace",
                    new=self._fake_launch_arc_call,
                ),
                patch(
                    "forwin.genesis.handoff.map_bootstrap.create_or_update_book_map",
                    return_value=invalid_map,
                ),
                self.assertRaises(GenesisMapBootstrapError) as raised,
            ):
                self._start_writing(service, session, project.id)
            session.commit()

            project_row = session.get(Project, project.id)
            arcs = (
                session.execute(
                    select(ArcPlanVersion).where(
                        ArcPlanVersion.project_id == project.id
                    )
                )
                .scalars()
                .all()
            )
            chapters = (
                session.execute(
                    select(ChapterPlan).where(ChapterPlan.project_id == project.id)
                )
                .scalars()
                .all()
            )
            event_types = [
                event.event_type
                for event in session.execute(
                    select(DecisionEvent).where(DecisionEvent.project_id == project.id)
                ).scalars()
            ]
            failure = session.execute(
                select(DecisionEvent).where(
                    DecisionEvent.project_id == project.id,
                    DecisionEvent.event_type
                    == DecisionEventType.MAP_GENERATION_FAILED,
                )
            ).scalar_one()

        message = str(raised.exception)
        for word in ("map", "地图", "BookMap"):
            self.assertNotIn(word.lower(), message.lower())
        self.assertIsInstance(raised.exception, HandoffStageError)
        self.assertEqual(raised.exception.issues, ("anchor node unreachable",))
        assert project_row is not None
        self.assertEqual(project_row.creation_status, "genesis_ready")
        self.assertEqual(arcs, [])
        self.assertEqual(chapters, [])
        self.assertNotIn(DecisionEventType.START_WRITING_REQUESTED, event_types)
        self.assertEqual(
            json.loads(failure.payload_json)["issues"], ["anchor node unreachable"]
        )

    def test_arc_plan_versions_persist_blueprint_goal_stakes_payoff(self) -> None:
        service = self._service()
        with self.session_factory() as session:
            project = self._create_ready_project(
                session, service, project_id="proj-handoff-arc-fields"
            )
            with patch(
                "forwin.genesis.BookGenesisService._call_json_with_trace",
                new=self._fake_launch_arc_call,
            ):
                self._start_writing(service, session, project.id)
            session.commit()
            arcs = (
                session.execute(
                    select(ArcPlanVersion)
                    .where(ArcPlanVersion.project_id == project.id)
                    .order_by(ArcPlanVersion.arc_number.asc())
                )
                .scalars()
                .all()
            )
            fields = [
                (arc.title, arc.goal, arc.stakes, arc.payoff_direction)
                for arc in arcs
            ]
            payload = _arc_payload_from_row(arcs[0])

        self.assertEqual(
            fields,
            [
                ("旧城开局", "立主冲突", "会失去立足点与关键同伴", "局部揭秘"),
                ("遗迹升级", "升级世界冲突", "旧术外泄导致更大灾难", "更大悬念"),
            ],
        )
        self.assertEqual(
            (
                payload["title"],
                payload["goal"],
                payload["stakes"],
                payload["payoff_direction"],
            ),
            fields[0],
        )

    def test_handoff_retry_reuses_seed(self) -> None:
        service = self._service()
        with self.session_factory() as session:
            project = self._create_ready_project(
                session, service, project_id="proj-handoff-seed-retry"
            )
            revision = service.active_revision(session, project)
            pack = service.load_pack(revision)
            # An earlier attempt already built the map and committed the seed.
            GenesisMapBootstrap().bootstrap_book_map_from_genesis(
                session=session,
                updater=StateUpdater(session),
                project=project,
                revision=revision,
                pack=pack,
                decision_event_id="",
            )
            WorldSeedCommitter().commit(
                session=session, project=project, revision=revision, pack=pack
            )
            session.commit()

            with patch(
                "forwin.genesis.BookGenesisService._call_json_with_trace",
                new=self._fake_launch_arc_call,
            ):
                result = self._start_writing(service, session, project.id)
            session.commit()
            delta_count = session.execute(
                select(func.count(GraphDeltaRow.id)).where(
                    GraphDeltaRow.project_id == project.id
                )
            ).scalar_one()
            committed_event_count = session.execute(
                select(func.count(DecisionEvent.id)).where(
                    DecisionEvent.project_id == project.id,
                    DecisionEvent.event_type
                    == DecisionEventType.WORLD_SEED_COMMITTED,
                )
            ).scalar_one()
            project_row = session.get(Project, project.id)

        self.assertIs(result.world_seed_summary["skipped"], True)
        self.assertEqual(
            result.world_seed_summary["actor_count"],
            len(seeded_world_seed()["actors"]),
        )
        self.assertEqual(delta_count, 3)
        self.assertEqual(committed_event_count, 1)
        assert project_row is not None
        self.assertEqual(project_row.creation_status, "writing")

    def test_handoff_reuses_existing_arc_and_chapter_rows(self) -> None:
        service = self._service()
        with self.session_factory() as session:
            project = self._create_ready_project(
                session, service, project_id="proj-handoff-idempotent"
            )
            arc = ArcPlanVersion(
                id=new_id(),
                project_id=project.id,
                version=1,
                arc_number=1,
                arc_synopsis="已存在 arc",
                status="active",
                planned_target_size=3,
            )
            chapter = ChapterPlan(
                id=new_id(),
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="已存在章节",
                status="planned",
            )
            session.add_all([arc, chapter])
            session.flush()
            updater = StateUpdater(session)

            with patch(
                "forwin.genesis.BookGenesisService._call_json_with_trace",
                new=self._fake_launch_arc_call,
            ):
                result = service.handoff.start_writing(
                    session=session,
                    updater=updater,
                    command=StartWritingCommand(
                        project_id=project.id, actor_type="manual_ui"
                    ),
                )
            session.commit()

            arcs = (
                session.execute(
                    select(ArcPlanVersion).where(
                        ArcPlanVersion.project_id == project.id
                    )
                )
                .scalars()
                .all()
            )
            chapters = (
                session.execute(
                    select(ChapterPlan).where(ChapterPlan.project_id == project.id)
                )
                .scalars()
                .all()
            )

        self.assertEqual(result.active_chapter_plan_count, 1)
        self.assertEqual(len(arcs), 1)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].title, "已存在章节")
