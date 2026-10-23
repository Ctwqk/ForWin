"""Run control plans each band just in time and activates the next arc before that arc's first chapter."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from forwin.api_schema import ProjectExtendGenerationRequest
from forwin.application.projects import generation as project_generation
from forwin.application.read_models import build_project_detail
from forwin.book_state import BookStateRepository
from forwin.generation.continue_workset import ContinueWorkset, build_continue_generation_workset
from forwin.generation.pipeline_core.project_chapters import ChapterExecutionStage
from forwin.generation.pipeline_core.run_control import RunControlStage
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter
from forwin.maintenance.post_canon import save_world_prompt_trace
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.planning_v6 import ArcBeatSheetRow
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.arc_progression import ArcProgressionService
from forwin.planning.band_planner.service import BandPlanner
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.service import ArcBeatSheetService
from forwin.protocol.book_state import MapNode
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url
from tests.test_arc_beat_sheet import StubLLM
from tests.test_band_planner import HookedLLM, StubAudit, _band
from tests.test_chapter_delta_translator import REGION, SUBWORLD, _World, _pack


POLICY = RuntimePolicy.for_profile("standard")


@pytest.fixture
def Session():
    engine = get_engine(postgres_test_url("run-control-band-planning"))
    init_db(engine)
    yield get_session_factory(engine)
    engine.dispose()


def _book(Session, *, arcs: list[tuple[int, int, str]], target: int) -> tuple[_World, list[str]]:
    """A writing book with chapter-0 canon from a world seed and one arc per (start, end, status)."""
    pack = _pack()
    with Session.begin() as session:
        project = StateUpdater(session).create_project(
            title="run control", premise="run control", genre="test", runtime_policy=POLICY,
            creation_status="writing", target_total_chapters=target,
        )
        revision = BookGenesisRevision(project_id=project.id, pack_json=json.dumps(pack, ensure_ascii=False))
        session.add(revision)
        session.flush()
        project.active_genesis_revision_id = revision.id
        world = _World(project.id)
        repo = BookStateRepository(session)
        for number in (1, 2, 3):
            repo.create_map_node(MapNode(
                id=world.loc(number), project_id=project.id, subworld_id=SUBWORLD, region_id=REGION,
                node_type="site", name=f"site-{number}", metadata={"source_node_id": f"node-{number}"},
            ))
        session.flush()
        WorldSeedCommitter().commit(session=session, project=project, revision=revision, pack=pack)
        arc_ids = []
        for number, (start, end, status) in enumerate(arcs, start=1):
            arc = ArcPlanVersion(
                project_id=project.id, arc_number=number, chapter_start=start, chapter_end=end, status=status,
                title=f"arc-{number}", goal=f"goal-{number}", arc_synopsis=f"synopsis-{number}",
            )
            session.add(arc)
            session.flush()
            arc_ids.append(arc.id)
        return world, arc_ids


def _beat_sheets(client=None) -> ArcBeatSheetService:
    """Without a client every sheet is the deterministic fallback: bands of the policy's six chapters."""
    return ArcBeatSheetService(client, save_trace=save_world_prompt_trace)


def _planner(client) -> BandPlanner:
    return BandPlanner(client, feasibility=StubAudit(), save_trace=save_world_prompt_trace)


def _sheet(Session, world: _World, arc_id: str) -> None:
    with Session.begin() as session:
        _beat_sheets().ensure_for_arc(
            session, project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id), policy=POLICY,
        )


def _accept(Session, world: _World, arc_id: str, numbers) -> None:
    with Session.begin() as session:
        updater = StateUpdater(session)
        for number in numbers:
            plan = updater.create_chapter_plan(world.project_id, arc_id, number, f"written-{number}", "", ["goal"])
            plan.status = "accepted"


def _plans(session, project_id: str) -> dict[int, ChapterPlan]:
    return {plan.chapter_number: plan for plan in session.scalars(
        select(ChapterPlan).where(ChapterPlan.project_id == project_id)
    )}


def _arc_statuses(session, project_id: str) -> dict[str, str]:
    return {arc.id: arc.status for arc in session.scalars(
        select(ArcPlanVersion).where(ArcPlanVersion.project_id == project_id)
    )}


class _LockProbe:
    """Records, from a session of its own, whether the project row is free while a model answers."""

    def __init__(self, Session, project_id: str, events: list[str], label: str) -> None:
        self.Session = Session
        self.project_id = project_id
        self.events = events
        self.label = label

    def __call__(self) -> None:
        with self.Session() as probe:
            try:
                probe.execute(select(Project).where(Project.id == self.project_id).with_for_update(
                    key_share=True, nowait=True,
                )).scalar_one()
            except OperationalError:
                self.events.append(f"{self.label}:locked")
            else:
                self.events.append(self.label)


class _Stage(RunControlStage, ChapterExecutionStage):
    """Run control's chapter loop up to the writer, which records the chapter it was handed and stops the run."""

    def __init__(self, *, band_planner, arc_progression, events: list[str]) -> None:
        self.band_planner = band_planner
        self.arc_progression = arc_progression
        self.policy = POLICY
        self.events = events
        self.progress: list[tuple[str, dict]] = []
        self.retrieval_broker = SimpleNamespace(
            last_observability_summary={},
            build_chapter_context=lambda _repo, _project_id, plan: SimpleNamespace(plan=plan),
        )

    def _project_policy(self, *_args):
        return POLICY

    def _abort_requested(self) -> bool:
        return False

    def _pause_requested(self) -> bool:
        return False

    def _recover_post_canon_before_chapter(self, **_kwargs) -> None:
        return None

    def _strict_progression_block(self, **_kwargs):
        return "", "", ""

    def _manual_boundary_checkpoint(self, *_args, **_kwargs):
        return None

    def _emit_progress(self, event: str, **payload) -> None:
        self.progress.append((event, payload))

    def _audit_current_plan_before_write(self, **kwargs):
        return kwargs["context"]

    def _write_chapter_with_attention_fallback(self, *, context, chapter_number, **_kwargs):
        self.events.append(f"write:{chapter_number}:{context.plan.title}")
        return None


def _run(Session, stage: _Stage, project_id: str, chapters):
    with Session() as session:
        return stage._run_project_chapters(
            session=session, repo=StateRepository(session), updater=StateUpdater(session), checker=SimpleNamespace(),
            project_id=project_id, chapter_numbers=list(chapters), requested_chapters=len(list(chapters)),
        )


def test_run_control_plans_band_before_first_chapter(Session) -> None:
    world, (arc_id,) = _book(Session, arcs=[(1, 12, "active")], target=12)
    _sheet(Session, world, arc_id)
    events: list[str] = []
    client = HookedLLM(_LockProbe(Session, world.project_id, events, "plan"), _band(world, range(1, 7)))
    stage = _Stage(band_planner=_planner(client), arc_progression=ArcProgressionService(_beat_sheets()), events=events)

    _run(Session, stage, world.project_id, range(1, 13))

    # The band is planned with no lock held, then the writer gets the new plan.
    assert events == ["plan", "write:1:title-1"]
    assert client.payload(0)["chapters_to_plan"] == [1, 2, 3, 4, 5, 6]
    with Session() as session:
        plans = _plans(session, world.project_id)
        assert {number: plan.status for number, plan in plans.items()} == dict.fromkeys(range(1, 7), "planned")


def test_run_pauses_before_unplannable_chapter(Session) -> None:
    world, (arc_id,) = _book(Session, arcs=[(1, 12, "active")], target=12)
    _sheet(Session, world, arc_id)
    events: list[str] = []
    # Both answers fail validation: the band cannot be planned.
    client = StubLLM({"chapters": []}, {"chapters": []})
    stage = _Stage(band_planner=_planner(client), arc_progression=ArcProgressionService(_beat_sheets()), events=events)

    result = _run(Session, stage, world.project_id, range(1, 13))

    assert (result.status, result.pause_reason) == ("needs_review", "world_conflict")
    assert (result.completed_chapters, result.paused_chapters) == ([], [1])
    assert events == []
    with Session() as session:
        conflict = session.scalar(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == world.project_id, WorldPlanConflictRow.source == "band_planning",
        ))
        assert (conflict.status, conflict.blocks_from_chapter, conflict.required_decider) == ("open", 1, "human")
        conflict_id = conflict.id
        assert _plans(session, world.project_id) == {}
        detail = build_project_detail(
            session=session, project=session.get(Project, world.project_id), display_datetime=str,
        )
    paused = [payload for _event, payload in stage.progress if payload.get("stage") == "paused_for_review"]
    assert [item["payload"] for item in paused] == [{"reason": "world_conflict", "conflict_id": conflict_id}]
    # The blocking reason is the conflict's, derived as P3 derives it.
    assert detail.blocking_reason.code == "world_conflict_pending"
    assert conflict_id in detail.blocking_reason.detail
    assert detail.generation_control.plan_state == "pending_band_plan"
    assert detail.generation_control.can_resume is False

    # While the conflict is open the run stops at it before asking the model again.
    again = _run(Session, stage, world.project_id, range(1, 13))
    assert again.paused_chapters == [1]
    assert len(client.calls) == 2


def test_run_control_activates_next_arc_when_plan_step_could_not(Session) -> None:
    world, (first, second) = _book(Session, arcs=[(1, 6, "active"), (7, 12, "planned")], target=12)
    _sheet(Session, world, first)
    _accept(Session, world, first, range(1, 7))
    events: list[str] = []
    # The sheet's model is down twice, so the arc gets the fallback sheet; each call checks the lock is free.
    sheet_client = HookedLLM(
        _LockProbe(Session, world.project_id, events, "sheet"), RuntimeError("down"), RuntimeError("down"),
    )
    band_client = HookedLLM(_LockProbe(Session, world.project_id, events, "plan"), _band(world, range(7, 13)))
    stage = _Stage(
        band_planner=_planner(band_client), arc_progression=ArcProgressionService(_beat_sheets(sheet_client)),
        events=events,
    )

    _run(Session, stage, world.project_id, range(7, 13))

    assert events == ["sheet", "sheet", "plan", "write:7:title-7"]
    with Session() as session:
        assert _arc_statuses(session, world.project_id) == {first: "completed", second: "active"}
        sheet = ArcBeatSheetRepository(session).active_for_arc(second)
        assert (sheet.source, sheet.version) == ("fallback", 1)
        plans = _plans(session, world.project_id)
        assert {number: plans[number].status for number in range(7, 13)} == dict.fromkeys(range(7, 13), "planned")


def test_run_control_finishes_an_activation_that_stopped_before_its_beat_sheet(Session) -> None:
    # The arc flip was committed, then the beat sheet failed: the next arc is active without a sheet.
    world, (first, second) = _book(Session, arcs=[(1, 6, "completed"), (7, 12, "active")], target=12)
    _accept(Session, world, first, range(1, 7))
    events: list[str] = []
    stage = _Stage(
        band_planner=_planner(StubLLM(_band(world, range(7, 13)))),
        arc_progression=ArcProgressionService(_beat_sheets()), events=events,
    )

    _run(Session, stage, world.project_id, range(7, 13))

    assert events == ["write:7:title-7"]
    with Session() as session:
        assert ArcBeatSheetRepository(session).active_for_arc(second).source == "fallback"
        assert _arc_statuses(session, world.project_id) == {first: "completed", second: "active"}


def test_run_control_replans_an_old_planner_row_in_an_arc_without_a_beat_sheet(Session) -> None:
    # As a book stands after deploy: the old planner activated the arc and wrote its rows, and no sheet exists.
    world, (arc_id,) = _book(Session, arcs=[(1, 12, "active")], target=12)
    _accept(Session, world, arc_id, range(1, 7))
    with Session.begin() as session:
        old_id = StateUpdater(session).create_chapter_plan(world.project_id, arc_id, 7, "old-7", "", ["old-goal"]).id
    events: list[str] = []
    stage = _Stage(
        band_planner=_planner(StubLLM(_band(world, range(7, 13)))),
        arc_progression=ArcProgressionService(_beat_sheets()), events=events,
    )

    _run(Session, stage, world.project_id, range(7, 13))

    assert events == ["write:7:title-7"]
    with Session() as session:
        sheet = ArcBeatSheetRepository(session).active_for_arc(arc_id)
        bands = [(band.chapter_start, band.chapter_end) for band in ArcBeatSheetRepository(session).load(sheet).band_breakdown]
        assert (sheet.source, bands) == ("fallback", [(1, 6), (7, 12)])
        plan = _plans(session, world.project_id)[7]
        # Planned again in place (Ruling P4a-R27): the same row, now in a band plan.
        assert (plan.id, plan.title) == (old_id, "title-7")
        assert plan.band_plan_id is not None


def test_extend_generation_adds_arc_and_beat_sheet_on_activation(Session) -> None:
    world, (first,) = _book(Session, arcs=[(1, 6, "active")], target=6)
    _sheet(Session, world, first)
    _accept(Session, world, first, range(1, 7))

    project_generation.extend_project_generation(
        world.project_id, ProjectExtendGenerationRequest(additional_chapters=6), get_session=Session,
        display_datetime=str, project_has_active_generation_task=lambda *_args, **_kwargs: False,
        generation_task_conflict_message=str,
    )

    with Session() as session:
        added = session.scalar(select(ArcPlanVersion).where(
            ArcPlanVersion.project_id == world.project_id, ArcPlanVersion.arc_number == 2,
        ))
        added_id = added.id
        # Only the arc skeleton: no beat sheet and no plans until the arc is activated.
        assert (added.status, added.chapter_start, added.chapter_end) == ("planned", 7, 12)
        assert ArcBeatSheetRepository(session).active_for_arc(added_id) is None
        assert sorted(_plans(session, world.project_id)) == list(range(1, 7))
        workset = build_continue_generation_workset(session, world.project_id)
    assert workset == ContinueWorkset(chapter_numbers=tuple(range(7, 13)), reason="chapters_pending")

    events: list[str] = []
    stage = _Stage(
        band_planner=_planner(StubLLM(_band(world, range(7, 13)))),
        arc_progression=ArcProgressionService(_beat_sheets()), events=events,
    )
    _run(Session, stage, world.project_id, workset.chapter_numbers)

    assert events == ["write:7:title-7"]
    with Session() as session:
        assert _arc_statuses(session, world.project_id) == {first: "completed", added_id: "active"}
        assert ArcBeatSheetRepository(session).active_for_arc(added_id) is not None


def test_arc_activation_is_idempotent_under_two_callers(Session) -> None:
    world, (first, second) = _book(Session, arcs=[(1, 6, "active"), (7, 12, "planned")], target=12)
    _sheet(Session, world, first)
    rival = ArcProgressionService(_beat_sheets())
    rival_results: list[str] = []

    def rival_activates() -> None:
        # Runs while the first caller's model answers, after its arc flip: no lock may still be held.
        with Session() as other:
            other.execute(text("SET lock_timeout = '5s'"))
            arc = rival.activate_next_arc_if_needed(other, project_id=world.project_id, after_chapter=6, policy=POLICY)
            rival_results.append(arc.id)

    first_caller = ArcProgressionService(_beat_sheets(HookedLLM(rival_activates, RuntimeError("down"), RuntimeError("down"))))
    with Session() as session:
        activated_id = first_caller.activate_next_arc_if_needed(
            session, project_id=world.project_id, after_chapter=6, policy=POLICY,
        ).id
    with Session() as session:
        again_id = rival.activate_next_arc_if_needed(
            session, project_id=world.project_id, after_chapter=6, policy=POLICY,
        ).id
        sheets = list(session.scalars(select(ArcBeatSheetRow).where(ArcBeatSheetRow.arc_plan_id == second)))
        statuses = _arc_statuses(session, world.project_id)
        traces = session.execute(select(PromptTrace.input_snapshot_json, PromptTrace.output_summary_json).where(
            PromptTrace.project_id == world.project_id, PromptTrace.stage_key == "plan.arc_beats",
        )).all()

    assert activated_id == again_id == second
    assert rival_results == [second, second]
    assert statuses == {first: "completed", second: "active"}
    # One sheet: the first caller's generation lost to the rival's under the lock and kept only its trace.
    assert [(row.version, row.status) for row in sheets] == [(1, "active")]
    second_traces = [json.loads(summary)["beat_sheet_id"] for snapshot, summary in traces
                     if json.loads(snapshot)["arc_plan_id"] == second]
    assert second_traces == [sheets[0].id, sheets[0].id]


def test_activation_needs_the_end_of_the_active_arc_and_a_following_arc(Session) -> None:
    world, (first, second) = _book(Session, arcs=[(1, 6, "active"), (7, 12, "planned")], target=12)
    service = ArcProgressionService(_beat_sheets())

    with Session() as session:
        mid_arc = service.activate_next_arc_if_needed(session, project_id=world.project_id, after_chapter=3, policy=POLICY)
        assert mid_arc is None
        assert _arc_statuses(session, world.project_id) == {first: "active", second: "planned"}
        assert service.activate_next_arc_if_needed(
            session, project_id=world.project_id, after_chapter=6, policy=POLICY,
        ).id == second
        # The last arc stays active at its end: no arc follows it.
        assert service.activate_next_arc_if_needed(
            session, project_id=world.project_id, after_chapter=12, policy=POLICY,
        ) is None
        assert _arc_statuses(session, world.project_id) == {first: "completed", second: "active"}
