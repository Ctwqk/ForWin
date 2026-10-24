from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from forwin.audit.events import DecisionEventType
from forwin.book_state import BookStateRepository
from forwin.book_state.projection import BookStateProjection
from forwin.genesis import BookGenesisService
from forwin.genesis.handoff.commands import HandoffStageError, StartWritingCommand
from forwin.genesis.handoff.map_bootstrap import (
    GenesisMapBootstrap,
    GenesisMapBootstrapError,
)
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter, seed_ref_id
from forwin.maintenance.post_canon import save_world_prompt_trace
from forwin.map.models import MapNodeRow
from forwin.map.protocol import BookMapGenerationResult, MapValidationReport
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.book_state import GraphDeltaRow
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.planning_v6 import ArcBeatSheetRow
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.planning.beat_sheet.service import ArcBeatSheetService
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.updater import StateUpdater
from tests.helpers.genesis_seed import (
    seeded_map_atlas,
    seeded_story_engine,
    seeded_world_seed,
)
from tests.postgres import postgres_test_url
from tests.test_arc_beat_sheet import StubLLM
from tests.test_band_planner import HookedLLM


class _NoApiKeyClient:
    api_key = ""
    codex_enabled = False
    profile_id = ""
    profile_name = ""
    model = "fake-model"
    base_url = ""
    last_call_result = None

    def chat(self, *_args, **_kwargs):  # pragma: no cover - start-writing makes no Genesis call
        raise AssertionError("handoff tests should not call the real LLM")


class GenesisHandoffServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = get_engine(postgres_test_url("genesis-handoff-service"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _service(self, beat_sheet_client=None) -> BookGenesisService:
        """Without ``beat_sheet_client`` the arc-1 beat sheet is the deterministic fallback."""
        return BookGenesisService(
            llm_client=_NoApiKeyClient(),
            beat_sheets=ArcBeatSheetService(beat_sheet_client, save_trace=save_world_prompt_trace),
        )

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
        ProjectPolicyStore(session).initialize(project, RuntimePolicy.for_profile("standard"))
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

    def test_start_writing_creates_beat_sheet_and_no_chapter_plans(self) -> None:
        service = self._service()
        with self.session_factory() as session:
            project = self._create_ready_project(
                session, service, project_id="proj-handoff-success"
            )
            updater = StateUpdater(session)
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
                    select(ChapterPlan).where(ChapterPlan.project_id == project.id)
                )
                .scalars()
                .all()
            )
            sheets = (
                session.execute(
                    select(ArcBeatSheetRow).where(
                        ArcBeatSheetRow.project_id == project.id
                    )
                )
                .scalars()
                .all()
            )
            sheet_trace_count = session.execute(
                select(func.count(PromptTrace.id)).where(
                    PromptTrace.project_id == project.id,
                    PromptTrace.stage_key == "plan.arc_beats",
                )
            ).scalar_one()
            constraint_deltas = (
                session.execute(
                    select(GraphDeltaRow).where(
                        GraphDeltaRow.project_id == project.id,
                        GraphDeltaRow.source_type == "plan_constraint",
                    )
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
        # requested_chapters for the first run: arc 1's chapter count, with no plan rows behind it.
        self.assertEqual(result.active_arc_chapter_count, 3)
        self.assertEqual(len(arcs), 2)
        self.assertEqual(arcs[0].status, "active")
        self.assertEqual(arcs[0].planned_target_size, 3)
        self.assertEqual(arcs[1].status, "planned")
        self.assertEqual(chapters, [])
        # Only arc 1 gets a beat sheet; the later arcs get theirs on activation.
        self.assertEqual(
            [(sheet.arc_plan_id, sheet.source, sheet.status) for sheet in sheets],
            [(arcs[0].id, "fallback", "active")],
        )
        self.assertEqual(sheet_trace_count, 1)
        self.assertEqual(
            [(delta.chapter_number, delta.source_id) for delta in constraint_deltas],
            [(0, sheets[0].id)],
        )
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

    def test_start_writing_commits_the_beat_sheet_protections_at_chapter_zero(
        self,
    ) -> None:
        project_id = "proj-handoff-protections"
        hero = seed_ref_id(project_id, "character", "cast-hero")
        sheet = {
            "protagonist_ref": hero,
            "phases": [{"name": "phase", "chapter_range": [1, 3], "purpose": "purpose"}],
            "protections": [{"ref": hero, "kind": "must_be_alive", "until_chapter": 3}],
            "band_breakdown": [{"band_index": 0, "chapter_start": 1, "chapter_end": 3}],
        }
        service = self._service(StubLLM(sheet))
        with self.session_factory() as session:
            project = self._create_ready_project(session, service, project_id=project_id)
            self._start_writing(service, session, project.id)
            session.commit()
            sheet_row = session.execute(
                select(ArcBeatSheetRow).where(ArcBeatSheetRow.project_id == project.id)
            ).scalar_one()
            protections = [
                node
                for node in BookStateProjection(session)
                .load_runtime_as_of(project.id, as_of_chapter=0)
                .narrative.nodes_by_id.values()
                if node.node_type == "review_constraint"
            ]

        self.assertEqual(sheet_row.source, "llm")
        self.assertEqual(
            [(node.payload["ref"], node.payload["kind"], node.payload["until_chapter"])
             for node in protections],
            [(hero, "must_be_alive", 3)],
        )
        self.assertEqual(protections[0].metadata["beat_sheet_id"], sheet_row.id)

    def _start_writing(self, service: BookGenesisService, session, project_id: str):
        return service.handoff.start_writing(
            session=session,
            updater=StateUpdater(session),
            command=StartWritingCommand(project_id=project_id, actor_type="manual_ui"),
        )

    def test_start_writing_commits_world_seed_after_map_and_before_the_beat_sheet(
        self,
    ) -> None:
        observed: dict[str, object] = {}
        holder: dict[str, object] = {}

        def record_seeded_world() -> None:
            session, project_id = holder["session"], holder["project_id"]
            observed["character_names"] = sorted(
                node.name
                for node in BookStateRepository(session).list_world_nodes(project_id)
                if str(node.node_type) == "character"
            )
            observed["map_node_count"] = session.execute(
                select(func.count(MapNodeRow.id)).where(
                    MapNodeRow.project_id == project_id
                )
            ).scalar_one()

        # The sheet's model is down, so the arc falls back after the hook has looked at the world.
        service = self._service(
            HookedLLM(record_seeded_world, RuntimeError("down"), RuntimeError("down"))
        )
        with self.session_factory() as session:
            project = self._create_ready_project(
                session, service, project_id="proj-handoff-seed-order"
            )
            holder.update(session=session, project_id=project.id)
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

        self.assertEqual(
            fields,
            [
                ("旧城开局", "立主冲突", "会失去立足点与关键同伴", "局部揭秘"),
                ("遗迹升级", "升级世界冲突", "旧术外泄导致更大灾难", "更大悬念"),
            ],
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

            result = self._start_writing(service, session, project.id)
            session.commit()
            # The seed's deltas only: the beat sheet's protections add one of their own.
            delta_count = session.execute(
                select(func.count(GraphDeltaRow.id)).where(
                    GraphDeltaRow.project_id == project.id,
                    GraphDeltaRow.source_type != "plan_constraint",
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
                chapter_start=1,
                chapter_end=3,
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

            result = service.handoff.start_writing(
                session=session,
                updater=updater,
                command=StartWritingCommand(
                    project_id=project.id, actor_type="manual_ui"
                ),
            )
            session.commit()

            sheet_arc_ids = (
                session.execute(
                    select(ArcBeatSheetRow.arc_plan_id).where(
                        ArcBeatSheetRow.project_id == project.id
                    )
                )
                .scalars()
                .all()
            )
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

        self.assertEqual(result.active_arc_chapter_count, 3)
        self.assertEqual(len(arcs), 1)
        self.assertEqual(sheet_arc_ids, [arc.id])
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].title, "已存在章节")
