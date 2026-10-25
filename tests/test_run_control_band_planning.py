"""Run control plans each band just in time and activates the next arc before that arc's first chapter."""

from __future__ import annotations

from unittest.mock import Mock
from forwin.generation import continue_workset

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


def _continue(Session, stage, project_id, **kwargs):
    stage._SessionFactory = Session
    stage._make_state_helpers = lambda session: (StateRepository(session), StateUpdater(session), SimpleNamespace())
    stage._bind_audit_context = lambda **_: None
    stage._clear_audit_context = lambda: None
    stage._record_decision_event = lambda **_: None
    stage.arc_envelope_manager = SimpleNamespace(
        ensure_active_arc_resolution=lambda **_: pytest.fail("continue must not call old arc activation"),
    )
    return stage.continue_project(project_id, **kwargs)


def _reset_with_draft(Session, world, number, *, status="planned"):
    from forwin.models.draft import ChapterDraft
    with Session.begin() as session:
        plan = _plans(session, world.project_id)[number]
        session.add(ChapterDraft(chapter_plan_id=plan.id, body_text="previous draft"))
        plan.status = status
        return plan.id, plan.title, plan.goals_json


def test_continue_historical_gap_never_hands_accepted_chapters_to_writer(Session):
    world, (arc,) = _book(Session, arcs=[(1, 6, "active")], target=6)
    _accept(Session, world, arc, range(1, 5))
    _reset_with_draft(Session, world, 2)
    events = []
    client = StubLLM(_band(world, (5, 6)))
    stage = _Stage(band_planner=_planner(client), arc_progression=ArcProgressionService(_beat_sheets()), events=events)
    for number in (2, 5, 6):
        _continue(Session, stage, world.project_id, max_chapters=1)
        with Session.begin() as session:
            _plans(session, world.project_id)[number].status = "accepted"
    assert events == ["write:2:written-2", "write:5:title-5", "write:6:title-6"]
    # A stale caller supplying accepted chapters is also safe.
    _run(Session, stage, world.project_id, (3, 4))
    assert events == ["write:2:written-2", "write:5:title-5", "write:6:title-6"]
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", ["planned", "failed"])
@pytest.mark.parametrize("arc_status", ["active", "completed"])
def test_drafted_old_plan_is_preserved_in_sheetless_arc(Session, status, arc_status):
    world, arcs = _book(Session, arcs=[(1, 6, arc_status), (7, 12, "active" if arc_status == "completed" else "planned")], target=12)
    _accept(Session, world, arcs[0], range(1, 7))
    original = _reset_with_draft(Session, world, 3, status=status)
    client = StubLLM()
    planner = _planner(client)
    with Session.begin() as session:
        plan = planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=3, policy=POLICY)
        assert (plan.id, plan.title, plan.goals_json) == original
        assert (plan.status, plan.band_plan_id) == (status, None)
    events = []
    stage = _Stage(band_planner=planner, arc_progression=ArcProgressionService(_beat_sheets()), events=events)
    _run(Session, stage, world.project_id, (3,))
    assert events == ["write:3:written-3"]
    assert client.calls == []
    with Session() as session:
        assert _arc_statuses(session, world.project_id) == {
            arcs[0]: arc_status, arcs[1]: "active" if arc_status == "completed" else "planned",
        }
        assert list(session.scalars(select(ArcBeatSheetRow).where(ArcBeatSheetRow.project_id == world.project_id))) == []


def test_historical_rewrite_continue_keeps_active_arc_and_later_plans_next_arc(Session):
    world, (first, second, third) = _book(
        Session, arcs=[(1, 6, "completed"), (7, 12, "active"), (13, 18, "planned")], target=18,
    )
    _accept(Session, world, first, range(1, 7))
    _accept(Session, world, second, range(7, 13))
    original = _reset_with_draft(Session, world, 3)
    events = []
    stage = _Stage(band_planner=_planner(StubLLM(_band(world, range(13, 19)))),
                   arc_progression=ArcProgressionService(_beat_sheets()), events=events)
    _continue(Session, stage, world.project_id, max_chapters=1)
    with Session.begin() as session:
        assert _arc_statuses(session, world.project_id) == {first: "completed", second: "active", third: "planned"}
        plan = _plans(session, world.project_id)[3]
        assert (plan.id, plan.title, plan.goals_json) == original
        plan.status = "accepted"
    _continue(Session, stage, world.project_id, max_chapters=1)
    assert events == ["write:3:written-3", "write:13:title-13"]
    with Session() as session:
        assert _arc_statuses(session, world.project_id) == {first: "completed", second: "completed", third: "active"}
        assert ArcBeatSheetRepository(session).active_for_arc(third) is not None
        assert _plans(session, world.project_id)[13].band_plan_id is not None


def test_run_control_recovers_arc_activation_that_lags_multiple_arcs(Session):
    world, (first, second, third) = _book(
        Session, arcs=[(1, 6, "active"), (7, 12, "planned"), (13, 18, "planned")], target=18,
    )
    _accept(Session, world, first, range(1, 7))
    _accept(Session, world, second, range(7, 13))
    stage = _Stage(band_planner=_planner(StubLLM(_band(world, range(13, 19)))),
                   arc_progression=ArcProgressionService(_beat_sheets()), events=[])
    _continue(Session, stage, world.project_id, max_chapters=1)
    assert stage.events == ["write:13:title-13"]
    with Session() as session:
        assert _arc_statuses(session, world.project_id) == {first: "completed", second: "completed", third: "active"}
        assert ArcBeatSheetRepository(session).active_for_arc(third) is not None
        assert _plans(session, world.project_id)[13].band_plan_id is not None


def test_arc_activation_serializes_callers_during_status_flip(Session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic
    from sqlalchemy import event, inspect

    world, (first, second) = _book(Session, arcs=[(1, 6, "active"), (7, 12, "planned")], target=12)
    locked, release, rival_started = Event(), Event(), Event()
    backend_ids, flips = [], []

    class HoldingProgression(ArcProgressionService):
        def _activate(self, session, **kwargs):
            arc = super()._activate(session, **kwargs)
            locked.set()
            assert release.wait(10), "test did not release the arc flip"
            return arc

    def record_flip(session, *_):
        for row in session.dirty:
            if isinstance(row, ArcPlanVersion) and row.status == "active" and inspect(row).attrs.status.history.has_changes():
                flips.append(row.id)

    def activate(service, *, rival=False):
        with Session() as session:
            event.listen(session, "before_flush", record_flip)
            session.execute(text("SET lock_timeout = '10s'"))
            if rival:
                backend_ids.append(session.scalar(text("SELECT pg_backend_pid()")))
                rival_started.set()
            return service.activate_next_arc_if_needed(
                session, project_id=world.project_id, after_chapter=6, policy=POLICY,
            ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(activate, HoldingProgression(_beat_sheets()))
        try:
            assert locked.wait(5)
            b = pool.submit(activate, ArcProgressionService(_beat_sheets()), rival=True)
            assert rival_started.wait(5)
            deadline = monotonic() + 5
            with Session() as observer:
                while not observer.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": backend_ids[0]}):
                    assert monotonic() < deadline, "second caller did not contend on the project lock"
                    assert not b.done()
                    release.wait(0.01)
        finally:
            release.set()
        assert a.result(timeout=10) == b.result(timeout=10) == second
    assert flips == [second]
    with Session() as session:
        assert _arc_statuses(session, world.project_id) == {first: "completed", second: "active"}
        sheets = list(session.scalars(select(ArcBeatSheetRow).where(ArcBeatSheetRow.arc_plan_id == second)))
        assert [(sheet.version, sheet.status) for sheet in sheets] == [(1, "active")]


def test_workset_skips_accepted_chapters_after_rewrite_gap(monkeypatch):
    session = Mock()
    session.get.return_value = SimpleNamespace(creation_status="writing")
    monkeypatch.setattr(continue_workset, "writable_chapter_ranges", lambda *_: {"book": range(1, 7)})
    plans = [SimpleNamespace(chapter_number=n, status="planned" if n == 2 else "accepted") for n in range(1, 5)]
    result = continue_workset.build_continue_generation_workset(session, "book", preloaded_plans=plans)
    assert result.chapter_numbers == (2, 5, 6)
    assert continue_workset.build_continue_generation_workset(
        session, "book", preloaded_plans=plans, max_chapters=2,
    ).chapter_numbers == (2, 5)


def test_activation_recovers_when_active_arc_is_behind():
    session = Mock()
    arcs = [SimpleNamespace(id=str(n), chapter_start=start, chapter_end=start + 5, status=status)
            for n, start, status in [(1, 1, "active"), (2, 7, "planned"), (3, 13, "planned")]]
    session.scalars.return_value = arcs
    result = ArcProgressionService._activate(session, project_id="book", after_chapter=12)
    assert result is arcs[2]
    assert [arc.status for arc in arcs] == ["completed", "completed", "active"]


def test_activation_never_moves_backwards():
    session = Mock()
    arcs = [SimpleNamespace(chapter_start=1, chapter_end=6, status="planned"),
            SimpleNamespace(chapter_start=7, chapter_end=12, status="active")]
    session.scalars.return_value = arcs
    assert ArcProgressionService._activate(session, project_id="book", after_chapter=2) is None
    assert [arc.status for arc in arcs] == ["planned", "active"]


@pytest.mark.parametrize("error_type", [KeyError, IndexError])
def test_planner_programming_error_is_not_retried(monkeypatch, error_type):
    from forwin.generation.pipeline_core import run_control
    session = Mock()
    repo = Mock()
    repo.get_active_arc_plan.return_value = SimpleNamespace(chapter_start=1, chapter_end=6)
    monkeypatch.setattr(run_control, "StateRepository", lambda _: repo)
    stage = RunControlStage()
    stage.band_planner = Mock()
    stage.band_planner.ensure_plan_for_chapter.side_effect = error_type("audit bug")
    stage.arc_progression = Mock()
    with pytest.raises(error_type):
        stage._ensure_chapter_plan(session=session, project_id="book", chapter_number=1, policy=Mock())
    assert stage.band_planner.ensure_plan_for_chapter.call_count == 1
    session.rollback.assert_not_called()
    stage.arc_progression.ensure_beat_sheet.assert_not_called()


@pytest.mark.parametrize("status", ["planned", "failed"])
@pytest.mark.parametrize("history", ["draft", "candidate"])
def test_old_plan_with_history_does_not_need_arc_or_model(status, history):
    from forwin.models.project import ChapterPlan
    from forwin.planning.band_planner.service import BandPlanner
    session = Mock()
    plan = ChapterPlan(id="old-plan", project_id="book", chapter_number=3, band_plan_id=None, status=status)
    session.scalar.side_effect = [plan, "draft-id"] if history == "draft" else [plan, None, "candidate-id"]
    client = Mock()
    planner = BandPlanner(None, feasibility=Mock(), save_trace=Mock())
    planner.llm_client = client
    assert planner.ensure_plan_for_chapter(session, project_id="book", chapter_number=3, policy=Mock()) is plan
    client.chat.assert_not_called()
    planner.save_trace.assert_not_called()
    assert session.scalar.call_count == (2 if history == "draft" else 3)


@pytest.mark.parametrize("plan_bands", [True, False])
def test_missing_plan_without_conflict_has_chapter_unplanned_reason(monkeypatch, plan_bands):
    from forwin.generation.pipeline_core import project_chapters
    from tests.test_run_control_band_planning import _Stage
    conflicts = Mock()
    conflicts.open_blocking.return_value = None
    monkeypatch.setattr(project_chapters, "WorldConflictRepository", lambda _: conflicts)
    stage = _Stage(band_planner=Mock(), arc_progression=Mock(), events=[])
    stage._ensure_chapter_plan = Mock(return_value=None)
    repo = Mock()
    repo.get_chapter_plan.return_value = None
    result = stage._run_project_chapters(session=Mock(), repo=repo, updater=Mock(), checker=Mock(),
                                        project_id="book", chapter_numbers=[1], requested_chapters=1, plan_bands=plan_bands)
    assert (result.pause_reason, result.paused_chapters) == ("chapter_unplanned", [1])
    payloads = [payload["payload"] for _, payload in stage.progress if payload.get("stage") == "paused_for_review"]
    assert payloads == [{"reason": "chapter_unplanned"}]
    assert stage.events == []


def test_chapter_loop_defensively_skips_accepted_plans(monkeypatch):
    from forwin.generation.pipeline_core import project_chapters
    from tests.test_run_control_band_planning import _Stage
    conflicts = Mock()
    conflicts.open_blocking.return_value = None
    monkeypatch.setattr(project_chapters, "WorldConflictRepository", lambda _: conflicts)
    stage = _Stage(band_planner=Mock(), arc_progression=Mock(), events=[])
    stage._ensure_chapter_plan = Mock(return_value=SimpleNamespace(status="accepted"))
    stage._strict_progression_block = Mock(side_effect=AssertionError("accepted chapters must not enter writing"))
    result = stage._run_project_chapters(session=Mock(), repo=Mock(), updater=Mock(), checker=Mock(),
                                        project_id="book", chapter_numbers=[3, 4], requested_chapters=2)
    assert result.failed_chapters == result.paused_chapters == []
    stage._strict_progression_block.assert_not_called()
    assert stage.events == []


def test_project_without_arcs_has_no_writable_range():
    session = Mock()
    session.execute.return_value = Mock(all=lambda: [])
    assert continue_workset.writable_chapter_ranges(session, ["book"]) == {}


@pytest.mark.parametrize("arc_row", [("book", 1, 0, 3), ("book", 8, 0, 3)])
def test_degenerate_arc_range_writes_nothing_and_warns(arc_row, caplog):
    session = Mock()
    session.execute.return_value = Mock(all=lambda: [arc_row])
    with caplog.at_level("WARNING", logger="forwin.generation.continue_workset"):
        ranges = continue_workset.writable_chapter_ranges(session, ["book"])
    assert list(ranges["book"]) == []
    assert "empty arc chapter range" in caplog.text


def test_historical_plan_before_active_arc_bypasses_activation_and_planning(monkeypatch):
    from forwin.generation.pipeline_core import run_control
    repo = Mock()
    repo.get_active_arc_plan.return_value = SimpleNamespace(chapter_start=7, chapter_end=12)
    original = SimpleNamespace(status="planned", title="original")
    repo.get_chapter_plan.return_value = original
    monkeypatch.setattr(run_control, "StateRepository", lambda _: repo)
    stage = RunControlStage()
    stage.band_planner, stage.arc_progression = Mock(), Mock()
    assert stage._ensure_chapter_plan(session=Mock(), project_id="book", chapter_number=3, policy=Mock()) is original
    stage.band_planner.ensure_plan_for_chapter.assert_not_called()
    stage.arc_progression.activate_next_arc_if_needed.assert_not_called()


def test_planning_neighbor_keeps_old_plan_with_draft_history():
    from forwin.models.project import ChapterPlan
    from forwin.planning.band_planner.service import _rewritable
    from forwin.planning.beat_sheet.types import BandSpan
    session = Mock()
    session.scalar.return_value = "previous-draft"
    plan = ChapterPlan(id="old-plan", project_id="book", chapter_number=2, band_plan_id=None, status="planned")
    assert _rewritable(session, BandSpan(band_index=1, chapter_start=1, chapter_end=3), {2: plan}, from_chapter=1) == [1, 3]
