from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from forwin.book_state import BookStateCompiler, BookStateRepository
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.band_planner.types import BandPlanOutcome, ChapterPlanDraft, PlanIssue
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import ArcBeatSheetData
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet, GraphDelta, MapEdge, MapNode, NarrativePatch, NodePatch, WorldNode,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url
from tests.test_chapter_delta_translator import REGION, SUBWORLD, _World, _pack


POLICY = RuntimePolicy.for_profile("standard")
SECRET = "secret truth-1"


@pytest.fixture
def Session():
    engine = get_engine(postgres_test_url("plan-feasibility"))
    init_db(engine)
    yield get_session_factory(engine)
    engine.dispose()


def _book(Session) -> tuple[_World, str]:
    """Chapter-0 canon: the hero at loc 1, the rival at loc 2; loc 1 -road, 2h- loc 2 -hidden route- loc 3.

    The 12-chapter arc's beat sheet has a hard milestone at chapters 3-4, the reader learning fact-1 (secret)
    from chapter 9, the rival in its exit state and three 4-chapter bands.
    """
    pack = _pack()
    pack["world_seed"]["facts"][0]["proposition"] = SECRET
    with Session.begin() as session:
        project = StateUpdater(session).create_project(
            title="plan feasibility", premise="plan feasibility", genre="test",
            runtime_policy=POLICY, creation_status="genesis_ready",
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
        repo.create_map_edge(MapEdge(
            id=world.road, project_id=project.id, subworld_id=SUBWORLD, from_node_id=world.loc(1),
            to_node_id=world.loc(2), edge_type="road", bidirectional=True, travel_time=2.0,
        ))
        repo.create_map_edge(MapEdge(
            id=world.hidden, project_id=project.id, subworld_id=SUBWORLD, from_node_id=world.loc(2),
            to_node_id=world.loc(3), edge_type="hidden_route", bidirectional=True, travel_time=1.0,
            discovered_by_default=False,
        ))
        session.flush()
        WorldSeedCommitter().commit(session=session, project=project, revision=revision, pack=pack)
        arc = ArcPlanVersion(
            project_id=project.id, arc_number=1, chapter_start=1, chapter_end=12, status="active",
            title="arc-title", goal="arc-goal", stakes="arc-stakes", payoff_direction="arc-payoff",
            arc_synopsis="arc-synopsis",
        )
        session.add(arc)
        session.flush()
        ArcBeatSheetRepository(session).create(project.id, arc.id, ArcBeatSheetData.model_validate({
            "protagonist_ref": world.hero,
            "phases": [{"name": "phase-a", "chapter_range": [1, 12], "purpose": "purpose-a"}],
            "milestones": [{"id": "milestone-1", "description": "milestone-description", "target_range": [3, 4],
                            "required_refs": [world.hero], "hard": True}],
            "reveal_targets": [{"fact_ref": world.fact("fact-1"), "observer": "reader", "target_state": "knows",
                                "range": [9, 12]}],
            "exit_state": [{"ref": world.rival, "field": "alive_status", "value": "alive"}],
            "band_breakdown": [
                {"band_index": index, "chapter_start": start, "chapter_end": start + 3}
                for index, start in enumerate((1, 5, 9))
            ],
        }), source="llm", reason="activation")
        return world, arc.id


def _draft(world: _World, number: int, **fields: Any) -> ChapterPlanDraft:
    return ChapterPlanDraft.model_validate({
        "chapter_number": number, "title": f"title-{number}", "one_line": f"one-line-{number}",
        "goals": [f"goal-{number}"], "pov_ref": world.hero, "onstage_refs": [world.hero],
        "location_refs": [world.loc(1)], "expected_span": {"amount": 2, "unit": "时"}, "hook": f"hook-{number}",
        **fields,
    })


def _audit(Session, world: _World, arc_id: str, plans) -> list[PlanIssue]:
    from forwin.planning.feasibility import FeasibilityAuditor

    with Session() as session:
        repo = ArcBeatSheetRepository(session)
        return FeasibilityAuditor().audit_plans(
            session, project_id=world.project_id, plans=list(plans),
            beat_sheet=repo.load(repo.active_for_arc(arc_id)), policy=POLICY,
        )


def _compile(Session, world: _World, *, node_patches=(), narrative_patches=(), cognition_patches=(),
             key: str = "setup") -> None:
    with Session.begin() as session:
        result = BookStateCompiler(session).compile(ApprovedGraphDeltaSet(
            project_id=world.project_id, chapter_number=0, graph_deltas=[GraphDelta(
                id=f"{key}-{world.project_id}", project_id=world.project_id, chapter_number=0,
                node_patches=list(node_patches), narrative_patches=list(narrative_patches),
                cognition_patches=list(cognition_patches),
            )],
        ))
        assert result.committed, result.blocked_reasons


def _set_rival(world: _World, field: str, old: Any, new: Any) -> NodePatch:
    return NodePatch(node_id=world.rival, node_type="character", op="set", field_path=f"state.{field}",
                     old_value=old, new_value=new)


def _protect(node_id: str, payload: dict[str, Any]) -> NarrativePatch:
    return NarrativePatch(target_ref=f"review_constraint:{node_id}", op="create", new_value={
        "node_type": "review_constraint", "title": node_id, "status": "active", "payload": payload,
    })


def _clock_id(world: _World) -> str:
    return f"clock-{world.project_id}"


def _dead_rival(Session, world: _World) -> None:
    _compile(Session, world, node_patches=[_set_rival(world, "alive_status", "alive", "dead")])


def _protected_rival(Session, world: _World) -> None:
    _compile(Session, world, narrative_patches=[
        _protect("protect-rival", {"ref": world.rival, "kind": "must_be_alive", "until_chapter": 8}),
    ])


def _reader_protected_fact(Session, world: _World) -> None:
    _compile(Session, world, narrative_patches=[_protect("protect-reader", {
        "ref": "reader", "kind": "must_not_know", "until_chapter": 8, "observer_ref": "reader",
        "fact_ref": world.fact("fact-2"),
    })])


def _public_clock(Session, world: _World) -> None:
    clock = WorldNode(
        id=_clock_id(world), project_id=world.project_id, node_type="clock", name="clock",
        profile={"label": "clock-label", "owner_ref": world.hero, "scope": "public", "on_expire": ""},
        state={"due_at": 1000 + 60, "status": "running"},
    )
    _compile(Session, world, node_patches=[NodePatch(
        node_id=clock.id, node_type="clock", op="create", new_value=clock.model_dump(mode="json"),
    )])


def _no_setup(Session, world: _World) -> None:
    return None


def _reveal(world: _World, fact: str, observer: str = "reader") -> dict[str, Any]:
    return {"reveal_directives": [{"id": "reveal-1", "fact_ref": world.fact(fact), "observer": observer,
                                   "target_state": "knows"}]}


_Case = tuple[Callable, Callable[[_World], list[ChapterPlanDraft]], Callable[[_World], tuple]]

_CASES: dict[str, _Case] = {
    "onstage_unavailable": (
        _dead_rival,
        lambda world: [_draft(world, 1, onstage_refs=[world.hero, world.rival])],
        lambda world: ("onstage_unavailable", "error", 1, world.rival),
    ),
    "location_unknown": (
        _no_setup,
        lambda world: [_draft(world, 1, location_refs=["ghost-location"])],
        lambda world: ("location_unknown", "error", 1, "ghost-location"),
    ),
    "location_unreachable": (
        # Two hours of road in a one-hour chapter, beyond the 20% travel tolerance.
        _no_setup,
        lambda world: [_draft(world, 1, location_refs=[world.loc(2)], expected_span={"amount": 1, "unit": "时"})],
        lambda world: ("location_unreachable", "error", 1, world.loc(2)),
    ),
    "location_route_unknown": (
        # The hero does not know the hidden route to loc 3.
        _no_setup,
        lambda world: [_draft(world, 1, location_refs=[world.loc(3)], expected_span={"amount": 1, "unit": "日"})],
        lambda world: ("location_unreachable", "error", 1, world.loc(3)),
    ),
    "reveal_too_early": (
        _no_setup,
        lambda world: [_draft(world, 1, **_reveal(world, "fact-1"))],
        lambda world: ("reveal_too_early", "error", 1, world.fact("fact-1")),
    ),
    "reveal_protected_from_reader": (
        # Ruling P4a-R13: a must_not_know protection whose observer is the reader.
        _reader_protected_fact,
        lambda world: [_draft(world, 1, **_reveal(world, "fact-2"))],
        lambda world: ("reveal_too_early", "error", 1, world.fact("fact-2")),
    ),
    "milestone_uncovered": (
        _no_setup,
        lambda world: [_draft(world, number) for number in range(1, 5)],
        lambda world: ("milestone_uncovered", "error", 3, "milestone-1"),
    ),
    "exit_state_unaddressed": (
        _no_setup,
        lambda world: [_draft(world, number, milestone_ids=["milestone-1"] if number == 9 else [],
                              intended_changes=[{"ref": world.rival, "field": "alive_status", "value": "dead"}])
                       for number in range(9, 13)],
        lambda world: ("exit_state_unaddressed", "warning", 12, world.rival),
    ),
    "clock_unanswered": (
        _public_clock,
        lambda world: [_draft(world, 1)],
        lambda world: ("clock_unanswered", "warning", 1, _clock_id(world)),
    ),
    "due_onstage_unbound": (
        # intent-1 needs the hero onstage two days after the opening; this chapter spans three.
        _no_setup,
        lambda world: [_draft(world, 1, expected_span={"amount": 3, "unit": "日"})],
        lambda world: ("due_onstage_unbound", "warning", 1, world.node("intent", "cast-1/intent-1")),
    ),
    "protection_conflict": (
        _protected_rival,
        lambda world: [_draft(world, 1, intended_changes=[
            {"ref": world.rival, "field": "alive_status", "value": "dead"},
        ])],
        lambda world: ("protection_conflict", "error", 1, world.rival),
    ),
    "protection_conflict_by_implied_move": (
        # Putting the rival onstage at loc 1 moves it off the location a protection holds it at.
        lambda Session, world: _compile(Session, world, narrative_patches=[_protect("hold-rival", {
            "ref": world.rival, "kind": "must_hold_state", "until_chapter": 8, "field": "location_id",
            "value": world.loc(2),
        })]),
        lambda world: [_draft(world, 1, onstage_refs=[world.hero, world.rival])],
        lambda world: ("protection_conflict", "error", 1, world.rival),
    ),
    "writer_text_states_secret": (
        # Ruling P4a-R21: the secret's proposition, its whitespace changed, in a goal.
        _no_setup,
        lambda world: [_draft(world, 1, goals=["learn that secret\n  truth-1 today"])],
        lambda world: ("writer_text_states_secret", "error", 1, world.fact("fact-1")),
    ),
    "writer_text_states_secret_with_inserted_space": (
        # A space inserted inside a word does not hide the proposition either.
        _no_setup,
        lambda world: [_draft(world, 1, hook="sec ret truth-1")],
        lambda world: ("writer_text_states_secret", "error", 1, world.fact("fact-1")),
    ),
    "hint_states_another_secret": (
        # A hint for the public fact may not state the secret one either.
        _no_setup,
        lambda world: [_draft(world, 1, hint_directives=[
            {"id": "hint-1", "fact_ref": world.fact("fact-2"), "hint_text": f"a hint: {SECRET}"},
        ])],
        lambda world: ("writer_text_states_secret", "error", 1, world.fact("fact-1")),
    ),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_feasibility_checks(Session, case: str) -> None:
    setup, plans, expected = _CASES[case]
    world, arc_id = _book(Session)
    setup(Session, world)

    issues = _audit(Session, world, arc_id, plans(world))

    code, severity, chapter, ref = expected(world)
    assert (code, severity, chapter, ref) in [
        (issue.code, issue.severity, issue.chapter_number, issue.ref) for issue in issues
    ]
    assert {issue.code for issue in issues} == {code}
    assert all(issue.message for issue in issues)


def test_feasible_band_has_no_issues(Session) -> None:
    world, arc_id = _book(Session)
    plans = [
        # Two hours of road in a two-hour chapter, then back again: within the tolerance.
        _draft(world, 1, location_refs=[world.loc(2)], onstage_refs=[world.hero, world.rival]),
        _draft(world, 2, location_refs=[world.loc(1)]),
        _draft(world, 3, milestone_ids=["milestone-1"]),
        # Reader reveals of the scheduled secret at its chapter, and a hint that only hints.
        _draft(world, 4, hint_directives=[{"id": "hint-1", "fact_ref": world.fact("fact-1"), "hint_text": "a hint"}]),
    ]

    assert _audit(Session, world, arc_id, plans) == []
    # The secret may be revealed, and its proposition stated, from its reveal chapter on.
    late = _draft(world, 9, goals=[f"learn {SECRET}"], milestone_ids=["milestone-1"], **_reveal(world, "fact-1"),
                  intended_changes=[{"ref": world.rival, "field": "alive_status", "value": "alive"}])
    assert _audit(Session, world, arc_id, [late]) == []


def test_reader_reveal_directive_lets_the_chapter_state_the_secret(Session) -> None:
    world, arc_id = _book(Session)
    with Session.begin() as session:
        sheet = ArcBeatSheetRepository(session).active_for_arc(arc_id)
        sheet.reveal_targets_json = "[]"

    # Unscheduled, the secret's reveal directive is allowed, and it lets the chapter state the secret.
    stated = _draft(world, 1, goals=[f"learn {SECRET}"], **_reveal(world, "fact-1"))
    assert _audit(Session, world, arc_id, [stated]) == []
    unrevealed = _draft(world, 1, goals=[f"learn {SECRET}"])
    assert [issue.code for issue in _audit(Session, world, arc_id, [unrevealed])] == ["writer_text_states_secret"]


def test_feasibility_allows_restrained_onstage_when_plan_releases_them(Session) -> None:
    world, arc_id = _book(Session)
    free = {"state": "free", "by_ref": "", "since": None}
    _compile(Session, world, node_patches=[_set_rival(world, "restraint", free, {
        "state": "imprisoned", "by_ref": world.hero, "since": 1000,
    })])
    onstage = {"onstage_refs": [world.hero, world.rival]}

    restrained = _audit(Session, world, arc_id, [_draft(world, 1, **onstage)])
    assert [(issue.code, issue.chapter_number, issue.ref) for issue in restrained] == [
        ("onstage_unavailable", 1, world.rival),
    ]
    assert "imprisoned" in restrained[0].message

    released = [_draft(world, 1, intended_changes=[{"ref": world.rival, "field": "restraint", "value": free}]),
                _draft(world, 2, **onstage)]
    assert _audit(Session, world, arc_id, released) == []
    assert _audit(Session, world, arc_id, [_draft(world, 1, **onstage, intended_changes=[
        {"ref": world.rival, "field": "state.restraint.state", "value": "free"},
    ])]) == []


def test_milestone_carried_by_an_accepted_chapter_is_covered(Session) -> None:
    world, arc_id = _book(Session)
    with Session.begin() as session:
        updater = StateUpdater(session)
        for number in (1, 2, 3):
            plan = updater.create_chapter_plan(world.project_id, arc_id, number, f"old-{number}", "", ["old-goal"])
            plan.status = "accepted"
            plan.milestone_ids_json = json.dumps(["milestone-1"] if number == 3 else [])

    assert _audit(Session, world, arc_id, [_draft(world, 4)]) == []


# The plan step's response to an infeasible band (Task 10 calls it).


class StubPlanner:
    def __init__(self, outcome: Callable[[str], BandPlanOutcome]) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def replan_remaining(self, session, **kwargs) -> BandPlanOutcome:
        self.calls.append(kwargs)
        return self.outcome(kwargs["band_plan_id"])


def _band_plan(Session, world: _World, arc_id: str, *, replan_count: int) -> str:
    with Session.begin() as session:
        sheet = ArcBeatSheetRepository(session).active_for_arc(arc_id)
        row = BandPlanRow(project_id=world.project_id, arc_plan_id=arc_id, beat_sheet_id=sheet.id, band_index=0,
                          chapter_start=1, chapter_end=4, status="active", version=1, replan_count=replan_count,
                          plan_json="{}")
        session.add(row)
        session.flush()
        return row.id


_ISSUES = [
    PlanIssue(code="due_onstage_unbound", severity="warning", chapter_number=1, ref="intent-x", message="w"),
    PlanIssue(code="milestone_uncovered", severity="error", chapter_number=3, ref="milestone-1", message="m"),
    PlanIssue(code="reveal_too_early", severity="error", chapter_number=2, ref="fact:x", message="r"),
]


def test_infeasible_band_is_replanned_under_the_cap(Session) -> None:
    from forwin.planning.feasibility import respond_to_infeasible_band

    world, arc_id = _book(Session)
    band_plan_id = _band_plan(Session, world, arc_id, replan_count=POLICY.planning.max_replans_per_band - 1)
    planner = StubPlanner(lambda _id: BandPlanOutcome(band_plan_id="band-plan-new"))
    with Session.begin() as session:
        response = respond_to_infeasible_band(
            session, band_planner=planner, project_id=world.project_id, band_plan_id=band_plan_id, from_chapter=2,
            issues=_ISSUES, policy=POLICY,
        )

    assert (response.band_plan_id, response.conflict_id, response.replanned) == ("band-plan-new", None, True)
    assert [(call["band_plan_id"], call["from_chapter"]) for call in planner.calls] == [(band_plan_id, 2)]
    assert planner.calls[0]["notes"] == ["chapter 2: [reveal_too_early] r", "chapter 3: [milestone_uncovered] m"]
    with Session() as session:
        assert session.scalars(select(WorldPlanConflictRow)).all() == []


@pytest.mark.parametrize("at_cap", [True, False], ids=["at_cap", "replan_fails"])
def test_infeasible_band_opens_a_human_conflict(Session, at_cap: bool) -> None:
    from forwin.planning.feasibility import respond_to_infeasible_band
    from forwin.world_engine.conflict_routing import available_options, route_conflict
    from forwin.world_engine.conflict_types import ConflictDraft

    world, arc_id = _book(Session)
    cap = POLICY.planning.max_replans_per_band
    band_plan_id = _band_plan(Session, world, arc_id, replan_count=cap if at_cap else 0)
    planner = StubPlanner(lambda _id: BandPlanOutcome(band_plan_id=None, issues=_ISSUES[1:]))
    with Session.begin() as session:
        response = respond_to_infeasible_band(
            session, band_planner=planner, project_id=world.project_id, band_plan_id=band_plan_id, from_chapter=1,
            issues=_ISSUES, policy=POLICY,
        )

    assert len(planner.calls) == (0 if at_cap else 1)
    assert (response.band_plan_id, response.replanned) == (band_plan_id, False)
    with Session() as session:
        row = session.get(WorldPlanConflictRow, response.conflict_id)
        # The first error by chapter decides what the conflict broke and which chapter it blocks.
        assert (row.source, row.violated, row.required_decider, row.blocks_from_chapter, row.status) == (
            "plan_feasibility", "reveal_schedule", "human", 2, "open",
        )
        assert json.loads(row.options_json) == ["amend_plan"]
        proposed = json.loads(row.proposed_change_json)
        assert (proposed["kind"], proposed["band"], proposed["band_plan_id"]) == ("plan_issues", [1, 4], band_plan_id)
        expected_codes = [issue.code for issue in (_ISSUES if at_cap else _ISSUES[1:])]
        assert [item["code"] for item in proposed["issues"]] == expected_codes
        draft = ConflictDraft(
            project_id=row.project_id, source=row.source, detected_after_chapter=row.detected_after_chapter,
            story_time=row.story_time, involved_refs=json.loads(row.involved_refs_json), violated=row.violated,
            impact_level=row.impact_level, summary=row.summary, proposed_change=proposed,
            blocks_from_chapter=row.blocks_from_chapter,
        )
        assert available_options(draft) == ("amend_plan",)
        assert route_conflict(draft, policy=POLICY) == "human"


@pytest.mark.parametrize(("code", "violated"), [
    ("reveal_too_early", "reveal_schedule"),
    ("milestone_uncovered", "milestone"),
    ("protection_conflict", "protection"),
    ("onstage_unavailable", "feasibility"),
    ("writer_text_states_secret", "feasibility"),
])
def test_conflict_violation_follows_the_first_error(code: str, violated: str) -> None:
    from forwin.planning.feasibility import violated_by

    issues = [PlanIssue(code=code, severity="error", chapter_number=5, message="x"),
              PlanIssue(code="reveal_too_early", severity="error", chapter_number=6, message="y")]
    assert violated_by(issues) == violated


# The pre-write check.


@pytest.fixture
def clean_future_audit(monkeypatch):
    from forwin.generation.pipeline_core import audit_control
    from forwin.planning.future_plan_audit import FuturePlanAuditRun

    class CleanAuditor:
        def __init__(self, **_kwargs) -> None:
            pass

        def audit_and_apply(self, *, project_id, current_chapter, trigger_stage, **_kwargs):
            return FuturePlanAuditRun(id="future-run", project_id=project_id, current_chapter=current_chapter,
                                      trigger_stage=trigger_stage, inspected_chapters=[current_chapter],
                                      status="pass")

    monkeypatch.setattr(audit_control, "FuturePlanAuditor", CleanAuditor)


def _stage(Session):
    """The pre-write audit's pipeline surroundings, with the real event recorder and session factory."""
    from forwin.generation.pipeline_core.audit_control import AuditControlStage

    stage = AuditControlStage()
    stage.policy = POLICY
    stage.llm_client = None
    stage.retrieval_broker = None
    stage._SessionFactory = Session
    stage._audit_task_id = ""
    stage._audit_root_event_id = ""
    return stage


def _prewrite(Session, world: _World, plan_id: str):
    """Run the pre-write audit as the chapter loop does: an error rolls the chapter's session back."""
    with Session() as session:
        try:
            result = _stage(Session)._audit_current_plan_before_write(
                session=session, repo=None, updater=StateUpdater(session), project_id=world.project_id,
                chapter_plan=session.get(ChapterPlan, plan_id), context="context", trigger_stage="pre_write",
            )
        except RuntimeError as exc:
            session.rollback()
            return exc
        session.commit()
        return result


def _feasibility_events(Session, world: _World) -> list[dict[str, Any]]:
    from forwin.audit.events import DecisionEventType
    from forwin.models.audit import DecisionEvent

    with Session() as session:
        rows = session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == world.project_id,
            DecisionEvent.event_type == DecisionEventType.FUTURE_PLAN_AUDIT_RUN,
        ).order_by(DecisionEvent.created_at, DecisionEvent.id))
        return [payload for row in rows if (payload := json.loads(row.payload_json)).get("audit") == "feasibility"]


def _persisted_plan(Session, world: _World, arc_id: str, number: int, **fields: str) -> str:
    with Session.begin() as session:
        plan = StateUpdater(session).create_chapter_plan(world.project_id, arc_id, number, "title", "one", ["goal"])
        plan.pov_ref = world.hero
        plan.onstage_refs_json = json.dumps([world.hero])
        plan.location_refs_json = json.dumps([world.loc(1)])
        plan.expected_span_minutes = 120
        for name, value in fields.items():
            setattr(plan, name, value)
        session.flush()
        return plan.id


def test_prewrite_feasibility_error_blocks_chapter(Session, clean_future_audit) -> None:
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    world, arc_id = _book(Session)
    _dead_rival(Session, world)
    plan_id = _persisted_plan(Session, world, arc_id, 1, onstage_refs_json=json.dumps([world.hero, world.rival]))

    raised = _prewrite(Session, world, plan_id)

    assert isinstance(raised, RuntimeError)
    assert str(raised).startswith("future_plan_audit_blocked:feasibility")
    assert f"onstage_unavailable={world.rival}" in str(raised)
    # What survives the chapter loop's rollback: a plan conflict run control pauses on, and the audit event.
    with Session() as session:
        conflict = WorldConflictRepository(session).open_blocking(world.project_id, next_chapter=1)
        assert (conflict.source, conflict.required_decider, conflict.blocks_from_chapter) == (
            "plan_feasibility", "human", 1,
        )
        assert json.loads(conflict.options_json) == ["amend_plan"]
        proposed = json.loads(conflict.proposed_change_json)
        assert (proposed["band"], proposed["band_plan_id"]) == ([1, 4], None)
        assert [(issue["code"], issue["ref"]) for issue in proposed["issues"]] == [("onstage_unavailable", world.rival)]
        assert "is dead" in proposed["issues"][0]["message"]
        conflict_id = conflict.id
    (event,) = _feasibility_events(Session, world)
    assert [issue["code"] for issue in event["issues"]] == ["onstage_unavailable"]
    assert "is dead" in event["issues"][0]["message"] and event["conflict_id"] == conflict_id

    # Blocking the chapter again reuses the open conflict.
    assert isinstance(_prewrite(Session, world, plan_id), RuntimeError)
    with Session() as session:
        assert [row.id for row in WorldConflictRepository(session).list(world.project_id, status="open")] == [
            conflict_id,
        ]


def test_prewrite_feasibility_warning_is_only_recorded(Session, clean_future_audit) -> None:
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    world, arc_id = _book(Session)
    _public_clock(Session, world)
    plan_id = _persisted_plan(Session, world, arc_id, 1)

    assert _prewrite(Session, world, plan_id) == "context"
    (event,) = _feasibility_events(Session, world)
    assert [(issue["code"], issue["severity"]) for issue in event["issues"]] == [("clock_unanswered", "warning")]
    assert event["gate_outcome"]["decision"] == "warn"
    with Session() as session:
        assert WorldConflictRepository(session).list(world.project_id) == []

    # A clean chapter records nothing.
    clean_id = _persisted_plan(Session, world, arc_id, 2, world_bindings_json=json.dumps([
        {"kind": "clock", "ref": _clock_id(world), "how": ""},
    ]))
    assert _prewrite(Session, world, clean_id) == "context"
    assert len(_feasibility_events(Session, world)) == 1


# Fix round 1: conflict dedup, reader-known facts, milestones beyond the plans.


def test_plan_feasibility_conflicts_are_not_stacked(Session) -> None:
    from forwin.planning.beat_sheet.types import BandSpan
    from forwin.planning.feasibility import open_plan_feasibility_conflict
    from forwin.world_engine.conflict_repository import WorldConflictRepository
    from forwin.world_engine.conflict_types import ConflictDraft

    world, _arc_id = _book(Session)

    def opened(band: BandSpan, chapter: int) -> str:
        with Session.begin() as session:
            return open_plan_feasibility_conflict(
                session, project_id=world.project_id, band=band, policy=POLICY,
                issues=[PlanIssue(code="onstage_unavailable", severity="error", chapter_number=chapter, ref="x",
                                  message="m")],
            ).id

    first_band = BandSpan(band_index=0, chapter_start=1, chapter_end=4)
    first = opened(first_band, 2)
    # An open conflict that already blocks the band from chapter 2 blocks chapter 3 too.
    assert opened(first_band, 3) == first
    # One blocking only from chapter 2 cannot block chapter 1.
    assert opened(first_band, 1) != first
    # The band planner's conflict over a band is a plan conflict too.
    with Session.begin() as session:
        planning = WorldConflictRepository(session).create(ConflictDraft(
            project_id=world.project_id, source="band_planning", detected_after_chapter=0, story_time=0,
            involved_refs=[], violated="feasibility", impact_level="major", summary="s",
            proposed_change={"kind": "plan_issues", "band": [5, 8], "issues": []}, blocks_from_chapter=5,
        ), required_decider="human", options=("amend_plan",)).id
    assert opened(BandSpan(band_index=1, chapter_start=5, chapter_end=8), 6) == planning


def test_a_secret_the_reader_already_knows_may_be_stated(Session) -> None:
    from forwin.book_state.projection import BookStateProjection
    from forwin.planning.beat_sheet.service import beat_sheet_issues
    from forwin.protocol.book_state import CognitionPatch

    world, arc_id = _book(Session)
    stated = _draft(world, 1, goals=[f"learn {SECRET}"])
    assert [issue.code for issue in _audit(Session, world, arc_id, [stated])] == ["writer_text_states_secret"]
    sheet = ArcBeatSheetData.model_validate({
        "protagonist_ref": world.hero,
        "phases": [{"name": "phase-a", "chapter_range": [1, 12], "purpose": f"tell {SECRET}"}],
        "band_breakdown": [{"band_index": 0, "chapter_start": 1, "chapter_end": 12}],
    })

    def sheet_issues() -> list[str]:
        with Session() as session:
            runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        return [issue for issue in beat_sheet_issues(sheet, chapter_start=1, chapter_end=12, runtime=runtime,
                                                     actor_ids={world.hero, world.rival})
                if "states the proposition" in issue]

    assert len(sheet_issues()) == 1
    # The reader's recorded knowledge makes the secret revealed, with or without a schedule.
    _compile(Session, world, cognition_patches=[CognitionPatch(
        observer_type="reader", observer_id="reader", op="merge", field_path="knowledge_by_ref",
        new_value={world.fact("fact-1"): "knows"}, evidence_refs=["knowledge:told:-:-"],
    )])

    assert _audit(Session, world, arc_id, [stated]) == []
    assert sheet_issues() == []


def test_hard_milestone_due_after_the_last_planned_chapter_raises_nothing(Session) -> None:
    world, arc_id = _book(Session)

    # milestone-1 is due by chapter 4; chapters 1 and 2 are all that is planned.
    assert _audit(Session, world, arc_id, [_draft(world, 1), _draft(world, 2)]) == []


# Ruling P4a-R21: one normalizer drops all whitespace.


def test_normalizer_drops_every_whitespace() -> None:
    from forwin.planning.fact_text import normalized_text, states_proposition

    assert normalized_text(" 甲 乙\n丙\t") == "甲乙丙"
    assert states_proposition("前文甲 乙丙后文", "甲乙丙")
    assert states_proposition("a hint: secret truth-1", "secret  truth-1")
    assert not states_proposition("anything", "  ")


def test_band_validation_uses_the_shared_normalizer() -> None:
    from forwin.planning.band_planner import validation
    from forwin.planning.fact_text import normalized_text

    assert validation._normalized is normalized_text


@pytest.mark.parametrize("llm_available", [True, False])
def test_container_builds_the_plan_amender(monkeypatch, llm_available: bool) -> None:
    from forwin.config import InfrastructureConfig
    from forwin.planning.amend import PlanAmender
    from forwin.planning.feasibility import FeasibilityAuditor
    from forwin.runtime import container as module
    from forwin.runtime.container import RuntimeContainer

    client = SimpleNamespace(chat=lambda messages, **kwargs: "")
    monkeypatch.setattr(RuntimeContainer, "_build_core_services",
                        lambda self: SimpleNamespace(infrastructure="infrastructure", policy=POLICY,
                                                     artifact_store=None))
    monkeypatch.setattr(RuntimeContainer, "_build_generation_services", lambda self: SimpleNamespace(llm_client=client))
    monkeypatch.setattr(module, "_llm_available", lambda infrastructure, policy: llm_available)

    def container(role: str) -> RuntimeContainer:
        return RuntimeContainer.from_config(InfrastructureConfig(minimax_api_key=""), policy=POLICY, role=role)

    amender = container("api").build_plan_amender()
    assert isinstance(amender, PlanAmender)
    expected = client if llm_available else None
    assert amender.band_planner.llm_client is expected and amender.beat_sheet_service.llm_client is expected
    assert isinstance(amender.band_planner.feasibility, FeasibilityAuditor)
    with pytest.raises(RuntimeError, match="generation"):
        container("publisher_worker").build_plan_amender()


@pytest.mark.parametrize("actual, expected, changes, as_of, mismatch", [
    ("alive", "alive", [], 12, False),
    ("dead", "alive", [("hero", "alive_status", "alive")], 12, True),
    ("dead", "alive", [("hero", "alive_status", "alive")], 8, False),
    ("alive", "alive", [("hero", "alive_status", "dead")], 8, True),
    (None, None, [], 12, True),
])
def test_exit_state_compares_values(actual, expected, changes, as_of, mismatch):
    from forwin.planning.feasibility import _exit_state_issues
    from tests.test_world_digest import _runtime

    runtime = _runtime()
    runtime.as_of_chapter = as_of
    if actual is not None:
        runtime.world.states_by_node_id["hero"]["alive_status"] = actual
    sheet = ArcBeatSheetData.model_validate({
        "protagonist_ref": "hero", "phases": [], "band_breakdown": [
            {"band_index": 0, "chapter_start": 9, "chapter_end": 12}],
        "exit_state": [{"ref": "hero", "field": "alive_status", "value": expected}],
    })
    chapter = SimpleNamespace(number=12, changes=changes)
    issues = _exit_state_issues(sheet, [chapter], [], runtime=runtime)
    assert [issue.code for issue in issues] == (["exit_state_unaddressed"] if mismatch else [])


@pytest.mark.parametrize("field", ["profile.species", "species", "state.restraint.state", "restraint.state"])
def test_exit_state_reads_nested_and_profile_fields(field):
    from forwin.planning.feasibility import _exit_state_issues
    from tests.test_world_digest import _runtime

    runtime = _runtime()
    runtime.as_of_chapter = 12
    runtime.world.nodes_by_id["hero"].profile["species"] = "expected"
    runtime.world.states_by_node_id["hero"]["restraint"] = {"state": "expected"}
    sheet = ArcBeatSheetData.model_validate({
        "protagonist_ref": "hero", "phases": [], "band_breakdown": [
            {"band_index": 0, "chapter_start": 9, "chapter_end": 12}],
        "exit_state": [{"ref": "hero", "field": field, "value": "expected"}],
    })
    assert _exit_state_issues(sheet, [SimpleNamespace(number=12, changes=[])], [], runtime=runtime) == []
