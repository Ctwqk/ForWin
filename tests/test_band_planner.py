from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state import BookStateCompiler, BookStateRepository
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter
from forwin.maintenance.post_canon import save_world_prompt_trace
from forwin.models import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import ChapterDraft
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.phase import BandExperiencePlan
from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import ArcBeatSheetData, BandSpan
from forwin.protocol.book_state import ApprovedGraphDeltaSet, FactPatch, GraphDelta, MapNode, NodePatch, WorldNode
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_types import ConflictDecision, ConflictDraft
from forwin.world_engine.superseded_resolutions import supersede_resolutions
from forwin.writer.prompt_core.planning import WORLD_DIGEST_TITLES
from forwin.writer.prompt_core.world_engine import PLOT_RESPONSE_GENERIC_GOAL
from tests.postgres import postgres_test_url
from tests.test_arc_beat_sheet import StubLLM
from tests.test_chapter_delta_translator import OPENING, REGION, SUBWORLD, _World, _pack


POLICY = RuntimePolicy.for_profile("standard")
DAY = 24 * 60


@pytest.fixture
def Session():
    engine = get_engine(postgres_test_url("band-planner"))
    init_db(engine)
    yield get_session_factory(engine)
    engine.dispose()


@pytest.fixture
def digest_calls(monkeypatch) -> list[dict]:
    """The keyword arguments of every world digest the planner builds; the real digest still runs."""
    from forwin.planning.band_planner import inputs as module

    calls: list[dict] = []
    real = module.build_world_digest

    def spy(session, **kwargs):
        calls.append(kwargs)
        return real(session, **kwargs)

    monkeypatch.setattr(module, "build_world_digest", spy)
    return calls


class HookedLLM(StubLLM):
    """A stub model that runs ``hook`` before each answer, as a concurrent writer would."""

    def __init__(self, hook, *responses):
        super().__init__(*responses)
        self.hook = hook

    def chat(self, messages, **kwargs):
        self.hook()
        return super().chat(messages, **kwargs)


class StubAudit:
    """A feasibility auditor returning the given issue lists in turn, then none."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[list] = []

    def audit_plans(self, session, *, project_id, plans, beat_sheet, policy, runtime=None):
        self.calls.append(list(plans))
        return self.results.pop(0) if self.results else []


def _intent(world: _World) -> str:
    return world.node("intent", "cast-1/intent-1")


def _book(Session) -> tuple[_World, str, str]:
    """Chapter-0 canon from a world seed, a 12-chapter arc and its beat sheet of three 4-chapter bands."""
    pack = _pack()
    pack["world_seed"]["facts"][0]["proposition"] = "secret truth-1"
    with Session.begin() as session:
        project = StateUpdater(session).create_project(
            title="band planner", premise="band planner", genre="test",
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
        session.flush()
        WorldSeedCommitter().commit(session=session, project=project, revision=revision, pack=pack)
        arc = ArcPlanVersion(
            project_id=project.id, arc_number=1, chapter_start=1, chapter_end=12, status="active",
            title="arc-title", goal="arc-goal", stakes="arc-stakes", payoff_direction="arc-payoff",
            arc_synopsis="arc-synopsis",
        )
        session.add(arc)
        session.flush()
        sheet = ArcBeatSheetRepository(session).create(project.id, arc.id, ArcBeatSheetData.model_validate({
            "protagonist_ref": world.hero,
            "phases": [
                {"name": "phase-a", "chapter_range": [1, 4], "purpose": "purpose-a"},
                {"name": "phase-b", "chapter_range": [5, 12], "purpose": "purpose-b"},
            ],
            "milestones": [
                {"id": "milestone-1", "description": "milestone-description", "target_range": [3, 4],
                 "required_refs": [world.hero], "hard": True},
                {"id": "milestone-far", "description": "milestone-far-description", "target_range": [12, 12]},
            ],
            "band_breakdown": [
                {"band_index": 0, "chapter_start": 1, "chapter_end": 4},
                {"band_index": 1, "chapter_start": 5, "chapter_end": 8},
                {"band_index": 2, "chapter_start": 9, "chapter_end": 12},
            ],
            "reader_promise": {"genre_promise": "promise-value"},
        }), source="llm", reason="activation")
        return world, arc.id, sheet.id


def _chapter(world: _World, number: int, **fields) -> dict:
    return {
        "chapter_number": number, "title": f"title-{number}", "one_line": f"one-line-{number}",
        "goals": [f"goal-{number}"], "pov_ref": world.hero, "onstage_refs": [world.hero, world.rival],
        "location_refs": [world.loc(1)], "expected_span": {"amount": 2, "unit": "时"},
        "hook": f"hook-{number}", **fields,
    }


def _band(world: _World, numbers, changes: dict[int, dict] | None = None) -> dict:
    """A valid answer for ``numbers``; ``changes`` maps a chapter number to the fields that replace its own."""
    return {"chapters": [_chapter(world, number, **(changes or {}).get(number, {})) for number in numbers]}


def _planner(client, audit=None):
    from forwin.planning.band_planner.service import BandPlanner
    return BandPlanner(client, feasibility=audit or StubAudit(), save_trace=save_world_prompt_trace)


def _plan_band(Session, planner, world: _World, arc_id: str, sheet_id: str, band=(0, 1, 4), **kwargs):
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository as Repo
    index, start, end = band
    with Session.begin() as session:
        return planner.plan_band(
            session, project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id),
            beat_sheet_row=Repo(session).active_for_arc(arc_id), policy=POLICY,
            band=BandSpan(band_index=index, chapter_start=start, chapter_end=end), **kwargs,
        )


def _plans(session, project_id: str) -> dict[int, ChapterPlan]:
    return {plan.chapter_number: plan for plan in session.scalars(
        select(ChapterPlan).where(ChapterPlan.project_id == project_id)
    )}


def _traces(session, project_id: str) -> list[PromptTrace]:
    return list(session.scalars(select(PromptTrace).where(
        PromptTrace.project_id == project_id, PromptTrace.stage_key == "plan.band",
    ).order_by(PromptTrace.created_at, PromptTrace.id)))


def _trace_event_types(session, trace_id: str) -> list[str]:
    return list(session.scalars(select(DecisionEvent.event_type).where(
        DecisionEvent.related_object_type == "prompt_trace", DecisionEvent.related_object_id == trace_id,
    ).order_by(DecisionEvent.created_at, DecisionEvent.id)))


def _feedback(client, call: int) -> str:
    return client.user_prompt(call).split("Retry validation feedback", 1)[1]


def _pending_conflict(Session, world: _World, *, flag: bool = True, refs=("delta-1",)) -> str:
    """A world conflict resolved with plot_responds; ``flag`` marks it as waiting for a plan."""
    with Session.begin() as session:
        repo = WorldConflictRepository(session)
        row = repo.create(ConflictDraft(
            project_id=world.project_id, source="world_tick", detected_after_chapter=0, story_time=OPENING,
            involved_refs=[_intent(world), world.rival], violated="impact_review", impact_level="major",
            summary="conflict-author-summary", proposed_change={}, intent_ref=_intent(world),
            actor_ref=world.rival, blocks_from_chapter=1,
        ), required_decider="human", options=("plot_responds", "veto_world"))
        repo.mark_resolved(row, decision=ConflictDecision(
            option="plot_responds", decided_by="human", rationale="rationale", decided_at=datetime.now(UTC),
        ), result_refs=list(refs))
        if flag:
            decision = json.loads(row.decision_json)
            row.decision_json = json.dumps({**decision, "pending_plan_response": True})
        session.flush()
        return row.id


def test_plan_band_writes_plans_with_new_columns_and_experience(Session, digest_calls) -> None:
    world, arc_id, sheet_id = _book(Session)
    first = {
        "world_bindings": [{"kind": "intent", "ref": _intent(world), "how": "how-1"}],
        "reveal_directives": [{"id": "reveal-1", "fact_ref": world.fact("fact-1"), "observer": "reader",
                               "target_state": "suspects"}],
        "hint_directives": [{"id": "hint-1", "fact_ref": world.fact("fact-2"), "hint_text": "hint-text-1"}],
        "intended_changes": [{"ref": world.rival, "field": "alive_status", "value": "alive"}],
        "milestone_ids": ["milestone-1"],
        "location_refs": [world.loc(1), world.loc(2)],
        "expected_span": {"amount": 1.5, "unit": "日"},
    }
    client = StubLLM(_band(world, range(1, 5), {1: first}))
    audit = StubAudit()
    outcome = _plan_band(Session, _planner(client, audit), world, arc_id, sheet_id)

    assert outcome.band_plan_id is not None and outcome.conflict_id is None
    assert len(outcome.chapter_plan_ids) == 4 and outcome.issues == []
    assert len(client.calls) == 1
    kwargs = client.calls[0][1]
    assert (kwargs["stage_key"], kwargs["task_family"]) == ("plan.band", "planning")
    prompt = client.user_prompt(0)
    for expected in (WORLD_DIGEST_TITLES["protagonist"], "purpose-a", "milestone-description", "arc-goal"):
        assert expected in prompt
    payload = client.payload(0)
    assert payload["chapters_to_plan"] == [1, 2, 3, 4]
    assert [item["id"] for item in payload["milestones"]] == ["milestone-1"]
    assert world.rival in [item["id"] for item in payload["actors"]]
    assert world.loc(2) in [item["id"] for item in payload["locations"]]
    assert _intent(world) in [item["id"] for item in payload["intents"]]
    assert payload["experience_schedule"]["rewards"]
    # The digest: the policy budget, the opening minute plus one story day per chapter (no previous band).
    assert digest_calls == [{
        "project_id": world.project_id, "as_of_chapter": 0, "protagonist_ref": world.hero,
        "window_end_minute": OPENING + 4 * DAY, "budget_chars": POLICY.planning.digest_budget_chars,
        "runtime": digest_calls[0]["runtime"],
    }]
    assert [len(plans) for plans in audit.calls] == [4]

    with Session() as session:
        band_plan = session.get(BandPlanRow, outcome.band_plan_id)
        assert (band_plan.arc_plan_id, band_plan.beat_sheet_id, band_plan.band_index) == (arc_id, sheet_id, 0)
        assert (band_plan.chapter_start, band_plan.chapter_end) == (1, 4)
        assert (band_plan.status, band_plan.version, band_plan.replan_count) == ("active", 1, 0)
        assert [item["chapter_number"] for item in json.loads(band_plan.plan_json)["chapters"]] == [1, 2, 3, 4]
        plans = _plans(session, world.project_id)
        assert sorted(plans) == [1, 2, 3, 4]
        assert [plans[number].id for number in (1, 2, 3, 4)] == outcome.chapter_plan_ids
        plan = plans[1]
        assert (plan.arc_plan_id, plan.band_plan_id, plan.status) == (arc_id, outcome.band_plan_id, "planned")
        assert (plan.title, plan.one_line, json.loads(plan.goals_json)) == ("title-1", "one-line-1", ["goal-1"])
        assert plan.pov_ref == world.hero
        assert json.loads(plan.onstage_refs_json) == [world.hero, world.rival]
        assert json.loads(plan.location_refs_json) == [world.loc(1), world.loc(2)]
        assert json.loads(plan.world_bindings_json) == [{"kind": "intent", "ref": _intent(world), "how": "how-1"}]
        assert json.loads(plan.reveal_directives_json) == first["reveal_directives"]
        assert json.loads(plan.hint_directives_json) == first["hint_directives"]
        assert json.loads(plan.intended_changes_json) == first["intended_changes"]
        assert json.loads(plan.milestone_ids_json) == ["milestone-1"]
        assert (plan.expected_span_minutes, plan.hook) == (int(1.5 * DAY), "hook-1")
        assert plans[2].expected_span_minutes == 2 * 60
        experience = json.loads(plan.experience_plan_json)
        assert [(item["chapter_hint"], item["entity_name"]) for item in experience["chapter_entry_targets"]] == [
            (1, "甲"), (1, "乙"),
        ]
        assert experience["active_subworld_ids"] == [SUBWORLD]
        assert experience["progress_markers"]
        (band_experience,) = session.scalars(select(BandExperiencePlan).where(
            BandExperiencePlan.project_id == world.project_id,
        ))
        assert (band_experience.arc_id, band_experience.band_id) == (arc_id, "band:1:4")
        scheduled = json.loads(band_experience.schedule_json)["scheduled_rewards"]
        assert [item["chapter_hint"] for item in scheduled] == [
            item["chapter"] for item in payload["experience_schedule"]["rewards"]
        ]
        (trace,) = _traces(session, world.project_id)
        summary = json.loads(trace.output_summary_json)
        assert (summary["band_plan_id"], summary["failure_reason"]) == (outcome.band_plan_id, "")
        assert _trace_event_types(session, trace.id) == [DecisionEventType.LLM_REQUEST_SUCCEEDED]


def test_invalid_output_retries_once_with_errors(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    bad = _band(world, range(1, 5), {2: {"onstage_refs": [world.rival]}})
    client = StubLLM(bad, _band(world, range(1, 5)))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    assert len(client.calls) == 2
    assert "Retry validation feedback" not in client.user_prompt(0)
    feedback = _feedback(client, 1)
    assert "chapter 2" in feedback and "pov_not_onstage" in feedback and world.hero in feedback
    assert outcome.band_plan_id is not None and len(outcome.chapter_plan_ids) == 4
    with Session() as session:
        assert json.loads(_plans(session, world.project_id)[2].onstage_refs_json) == [world.hero, world.rival]
        (trace,) = _traces(session, world.project_id)
        assert DecisionEventType.RETRY_ATTEMPT in _trace_event_types(session, trace.id)


@pytest.mark.parametrize(("change", "code"), [
    ({"chapter_number": 9}, "chapter_numbers"),
    ({"location_refs": ["ghost-location"]}, "unknown_location"),
    ({"onstage_refs": ["ghost-actor"]}, "unknown_actor"),
    ({"milestone_ids": ["ghost-milestone"]}, "unknown_milestone"),
    ({"expected_span": {"amount": 1, "unit": "ghost-unit"}}, "unknown_time_unit"),
    ({"world_bindings": [{"kind": "clock", "ref": "ghost-clock", "how": ""}]}, "unknown_binding_ref"),
    ({"reveal_directives": [{"id": "r", "fact_ref": "fact:ghost", "observer": "reader",
                             "target_state": "knows"}]}, "unknown_fact"),
    ({"intended_changes": [{"ref": "ghost-actor", "field": "alive_status", "value": "dead"}]}, "unknown_change_ref"),
    ({"trope_ids": ["ghost-trope"]}, "unscheduled_trope"),
], ids=["chapter", "location", "actor", "milestone", "unit", "binding", "fact", "change", "trope"])
def test_unknown_refs_are_fed_back(Session, change, code) -> None:
    world, arc_id, sheet_id = _book(Session)
    client = StubLLM(_band(world, range(1, 5), {3: change}), _band(world, range(1, 5)))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    assert len(client.calls) == 2
    assert code in _feedback(client, 1)
    assert outcome.band_plan_id is not None


def test_infeasible_band_twice_creates_conflict_and_writes_no_plans(Session) -> None:
    from forwin.planning.band_planner.types import PlanIssue

    world, arc_id, _sheet_id = _book(Session)
    issue = PlanIssue(code="onstage_unavailable", severity="error", chapter_number=2, ref=world.rival,
                      message="the rival is restrained")
    client = StubLLM(_band(world, range(1, 5)), _band(world, range(1, 5)))
    audit = StubAudit([issue], [issue])
    planner = _planner(client, audit)
    with Session.begin() as session:
        assert planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=1,
                                               policy=POLICY) is None

    assert len(client.calls) == 2 and len(audit.calls) == 2
    assert "onstage_unavailable" in _feedback(client, 1)
    with Session() as session:
        assert _plans(session, world.project_id) == {}
        assert session.scalars(select(BandPlanRow).where(BandPlanRow.project_id == world.project_id)).all() == []
        (conflict,) = WorldConflictRepository(session).list(world.project_id)
        assert (conflict.source, conflict.violated, conflict.required_decider, conflict.status) == (
            "band_planning", "feasibility", "human", "open",
        )
        assert json.loads(conflict.options_json) == ["amend_plan"]
        assert conflict.blocks_from_chapter == 1
        assert json.loads(conflict.involved_refs_json) == [world.rival]
        proposed = json.loads(conflict.proposed_change_json)
        assert (proposed["kind"], proposed["band"]) == ("plan_issues", [1, 4])
        assert [(item["code"], item["chapter_number"], item["ref"]) for item in proposed["issues"]] == [
            ("onstage_unavailable", 2, world.rival),
        ]
        (trace,) = _traces(session, world.project_id)
        assert json.loads(trace.output_summary_json)["failure_reason"] == "infeasible"

    # The open conflict blocks the band: asking again neither calls the model nor adds a conflict.
    with Session.begin() as session:
        assert planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=3,
                                               policy=POLICY) is None
    assert len(client.calls) == 2
    with Session() as session:
        assert len(WorldConflictRepository(session).list(world.project_id)) == 1


def test_writer_facing_text_naming_a_concealed_entity_is_rejected(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    society = f"society-{world.project_id}"
    with Session.begin() as session:
        result = BookStateCompiler(session).compile(ApprovedGraphDeltaSet(
            project_id=world.project_id, chapter_number=0, graph_deltas=[GraphDelta(
                id=f"concealed-{world.project_id}", project_id=world.project_id, chapter_number=0,
                node_patches=[NodePatch(node_id=society, node_type="institution", op="create", new_value=WorldNode(
                    id=society, project_id=world.project_id, node_type="institution", name="hidden-society",
                    state={"visibility_level": "secret"},
                ).model_dump(mode="json"))],
                fact_patches=[FactPatch(fact_id="fact-society", op="create", proposition="society-truth",
                                        new_value={"project_id": world.project_id, "related_node_refs": [society]})],
            )],
        ))
        assert result.committed, result.blocked_reasons
    named = {"goals": ["find the hidden-society"], "hook": "hidden-society waits"}
    revealed = {**named, "reveal_directives": [{"id": "reveal-society", "fact_ref": "fact:fact-society",
                                               "observer": "reader", "target_state": "suspects"}]}
    client = StubLLM(_band(world, range(1, 5), {2: named}), _band(world, range(1, 5), {2: revealed}))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    feedback = _feedback(client, 1)
    assert "chapter 2: [writer_text_names_concealed] goals, hook name concealed entity" in feedback
    assert society in feedback
    # A chapter that reveals a fact about the entity may name it.
    assert outcome.band_plan_id is not None
    with Session() as session:
        assert json.loads(_plans(session, world.project_id)[2].goals_json) == ["find the hidden-society"]


def test_hint_text_containing_truth_is_rejected(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    # The fact's proposition, with its whitespace changed, inside a longer hint.
    hint = {"hint_directives": [{"id": "hint-1", "fact_ref": world.fact("fact-1"),
                                 "hint_text": "before secret\n  truth-1 after"}]}
    client = StubLLM(_band(world, range(1, 5), {1: hint}), _band(world, range(1, 5), {1: hint}))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    assert len(client.calls) == 2
    assert "hint_states_truth" in _feedback(client, 1)
    assert (outcome.band_plan_id, outcome.chapter_plan_ids, outcome.conflict_id) == (None, [], None)
    assert [(issue.code, issue.chapter_number) for issue in outcome.issues] == [("hint_states_truth", 1)]
    with Session() as session:
        assert _plans(session, world.project_id) == {}
        (trace,) = _traces(session, world.project_id)
        assert json.loads(trace.output_summary_json)["failure_reason"] == "invalid_output"


def test_replan_keeps_accepted_rows_and_reapplies_active_patches(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    conflict_id = _pending_conflict(Session, world)
    first = _band(world, range(1, 5), {3: {"goals": ["goal-3", PLOT_RESPONSE_GENERIC_GOAL]}})
    replanned = {"chapters": [
        _chapter(world, 3, title="title-3b", goals=["goal-3b"], hook="hook-3b"),
        _chapter(world, 4, title="title-4b", goals=["goal-4b"]),
    ]}
    client = StubLLM(first, replanned)
    planner = _planner(client)
    outcome = _plan_band(Session, planner, world, arc_id, sheet_id)
    with Session.begin() as session:
        plans = _plans(session, world.project_id)
        plans[1].status, plans[2].status = "accepted", "drafted"
        (patch,) = NarrativeObligationRepository(session).list_patches_for_plan(
            world.project_id, target_plan_id=plans[3].id,
        )
        before = {number: (plan.id, plan.title, plan.goals_json, plan.experience_plan_json, plan.band_plan_id)
                  for number, plan in plans.items()}
    # The answer already stated the response goal, so applying the patch left the goals as they were.
    assert json.loads(before[3][2]) == ["goal-3", PLOT_RESPONSE_GENERIC_GOAL]

    with Session.begin() as session:
        replan = planner.replan_remaining(
            session, project_id=world.project_id, band_plan_id=outcome.band_plan_id, from_chapter=2,
            policy=POLICY, notes=["note-value"],
        )

    payload = client.payload(1)
    # Chapter 2 is drafted, so only chapters 3 and 4 are planned again.
    assert payload["chapters_to_plan"] == [3, 4] and payload["notes"] == ["note-value"]
    assert payload["pending_responses"] == []
    assert [item["chapter_number"] for item in payload["kept_chapters"]] == [1, 2]
    assert replan.band_plan_id not in (None, outcome.band_plan_id)
    with Session() as session:
        plans = _plans(session, world.project_id)
        for number in (1, 2):
            plan = plans[number]
            assert (plan.id, plan.title, plan.goals_json, plan.experience_plan_json, plan.band_plan_id) == before[number]
        third = plans[3]
        assert third.id == before[3][0] and replan.chapter_plan_ids == [third.id, plans[4].id]
        assert (third.title, third.hook, third.band_plan_id) == ("title-3b", "hook-3b", replan.band_plan_id)
        # The plot-response patch still answers its resolved conflict, so it is applied again.
        assert json.loads(third.goals_json) == [PLOT_RESPONSE_GENERIC_GOAL, "goal-3b"]
        assert PLOT_RESPONSE_GENERIC_GOAL in json.loads(third.experience_plan_json)["progress_markers"]
        assert json.loads(plans[4].goals_json) == ["goal-4b"]
        old, new = session.get(BandPlanRow, outcome.band_plan_id), session.get(BandPlanRow, replan.band_plan_id)
        assert (old.status, new.status) == ("superseded", "active")
        assert (new.version, new.replan_count, new.band_index) == (2, 1, 0)
        assert [item["chapter_number"] for item in json.loads(new.plan_json)["chapters"]] == [1, 2, 3, 4]
        assert json.loads(session.get(WorldPlanConflictRow, conflict_id).result_refs_json) == [
            "delta-1", f"plan_patch:{patch.id}",
        ]
        assert len(_traces(session, world.project_id)) == 2


def test_a_chapter_drafted_during_the_model_call_is_not_rewritten(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    outcome = _plan_band(Session, _planner(StubLLM(_band(world, range(1, 5)))), world, arc_id, sheet_id)

    def draft_chapter_three() -> None:
        with Session.begin() as other:
            _plans(other, world.project_id)[3].status = "drafted"

    replanned = _band(world, (3, 4), {3: {"title": "title-3b"}, 4: {"title": "title-4b"}})
    with Session.begin() as session:
        replan = _planner(HookedLLM(draft_chapter_three, replanned)).replan_remaining(
            session, project_id=world.project_id, band_plan_id=outcome.band_plan_id, from_chapter=3,
            policy=POLICY, notes=[],
        )

    with Session() as session:
        plans = _plans(session, world.project_id)
        assert (plans[3].status, plans[3].title, plans[3].band_plan_id) == ("drafted", "title-3", outcome.band_plan_id)
        assert (plans[4].title, plans[4].band_plan_id) == ("title-4b", replan.band_plan_id)
        assert replan.chapter_plan_ids == [plans[4].id]
        chapters = json.loads(session.get(BandPlanRow, replan.band_plan_id).plan_json)["chapters"]
        assert [(item["chapter_number"], item["title"]) for item in chapters] == [
            (1, "title-1"), (2, "title-2"), (3, "title-3"), (4, "title-4b"),
        ]


def test_pending_plot_response_is_bound_and_attached(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    conflict_id = _pending_conflict(Session, world)
    # A P3-era plot response (no pending flag) and one a plan already answers are not pending.
    _pending_conflict(Session, world, flag=False)
    _pending_conflict(Session, world, refs=("delta-2", "plan_patch:patch-0"))
    with Session() as session:
        assert [row.id for row in WorldConflictRepository(session).pending_plot_responses(world.project_id)] == [
            conflict_id,
        ]
    bound = {"world_bindings": [{"kind": "intent", "ref": _intent(world), "how": "answer the change"}]}
    client = StubLLM(_band(world, range(1, 5)), _band(world, range(1, 5), {2: bound}))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    (pending,) = client.payload(0)["pending_responses"]
    assert (pending["id"], pending["goal"]) == (conflict_id, PLOT_RESPONSE_GENERIC_GOAL)
    assert set(pending["refs"]) == {_intent(world), world.rival}
    feedback = _feedback(client, 1)
    assert "pending_response_unaddressed" in feedback and conflict_id in feedback
    assert outcome.band_plan_id is not None
    with Session() as session:
        plans = _plans(session, world.project_id)
        (patch,) = NarrativeObligationRepository(session).list_patches_for_plan(
            world.project_id, target_plan_id=plans[2].id,
        )
        assert (patch.metadata["conflict_id"], patch.affected_chapters) == (conflict_id, [2])
        assert json.loads(plans[2].goals_json) == [PLOT_RESPONSE_GENERIC_GOAL, "goal-2"]
        conflict = session.get(WorldPlanConflictRow, conflict_id)
        assert json.loads(conflict.result_refs_json) == ["delta-1", f"plan_patch:{patch.id}"]
        assert WorldConflictRepository(session).pending_plot_responses(world.project_id) == []


def test_superseding_a_bound_plot_response_reverts_its_patch(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    conflict_id = _pending_conflict(Session, world)
    client = StubLLM(_band(world, range(1, 5), {1: {"goals": [PLOT_RESPONSE_GENERIC_GOAL, "goal-1"]}}))
    _plan_band(Session, _planner(client), world, arc_id, sheet_id)
    with Session.begin() as session:
        superseded = supersede_resolutions(session, project_id=world.project_id, graph_delta_id="delta-1",
                                           reason="rewritten")

    assert superseded.conflict_ids == (conflict_id,)
    with Session() as session:
        plan = _plans(session, world.project_id)[1]
        (patch, revert) = NarrativeObligationRepository(session).list_patches_for_plan(
            world.project_id, target_plan_id=plan.id,
        )
        assert superseded.reverted_plan_patch_ids == (patch.id,)
        assert revert.metadata["reverted_plan_patch_id"] == patch.id
        assert json.loads(plan.goals_json) == ["goal-1"]


@pytest.mark.parametrize("client", [
    StubLLM(RuntimeError("provider down"), RuntimeError("provider down")), None,
], ids=["provider_error", "no_client"])
def test_llm_outage_returns_no_plans_without_raising(Session, client) -> None:
    world, arc_id, sheet_id = _book(Session)
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    assert (outcome.band_plan_id, outcome.chapter_plan_ids, outcome.conflict_id) == (None, [], None)
    assert [issue.code for issue in outcome.issues] == ["llm_unavailable"]
    if client is not None:
        # A provider failure resends the same messages.
        assert len(client.calls) == 2 and client.calls[1][0] == client.calls[0][0]
    with Session() as session:
        assert _plans(session, world.project_id) == {}
        assert WorldConflictRepository(session).list(world.project_id) == []
        (trace,) = _traces(session, world.project_id)
        assert json.loads(trace.output_summary_json)["failure_reason"] == "llm_unavailable"
        failed = _trace_event_types(session, trace.id).count(DecisionEventType.LLM_REQUEST_FAILED)
        assert failed == (2 if client is not None else 0)


def test_ensure_plan_for_chapter_plans_its_band_from_the_previous_band(Session, digest_calls) -> None:
    world, arc_id, _sheet_id = _book(Session)
    with Session.begin() as session:
        updater = StateUpdater(session)
        for number in (1, 2, 3, 4):
            plan = updater.create_chapter_plan(world.project_id, arc_id, number, f"old-{number}", "", ["old-goal"])
            plan.status = "accepted"
            session.add(ChapterDraft(chapter_plan_id=plan.id, body_text="body", summary=f"summary-{number}"))
        result = BookStateCompiler(session).compile(ApprovedGraphDeltaSet(
            project_id=world.project_id, chapter_number=4, graph_deltas=[GraphDelta(
                id=f"chapter-4-{world.project_id}", project_id=world.project_id, chapter_number=4,
                story_time_start=OPENING, story_time_end=OPENING + 4 * 120,
                node_patches=[NodePatch(node_id=_intent(world), node_type="intent", op="set",
                                        field_path="state.status", old_value="planned", new_value="due")],
            )],
        ))
        assert result.committed, result.blocked_reasons
    client = StubLLM(_band(world, range(5, 9)))
    planner = _planner(client)
    with Session.begin() as session:
        plan = planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=6,
                                               policy=POLICY)
        plan_id = plan.id
        assert (plan.chapter_number, plan.title) == (6, "title-6")

    payload = client.payload(0)
    assert payload["chapters_to_plan"] == [5, 6, 7, 8]
    assert payload["band"] == {"band_index": 1, "chapter_start": 5, "chapter_end": 8}
    assert [phase["purpose"] for phase in payload["phases"]] == ["purpose-b"]
    # Milestones in the band and the next band's width around it.
    assert [item["id"] for item in payload["milestones"]] == ["milestone-1", "milestone-far"]
    assert payload["previous_band"]["accepted_chapters"] == [
        {"chapter_number": number, "title": f"old-{number}", "summary": f"summary-{number}"} for number in (1, 2, 3, 4)
    ]
    assert payload["due_onstage_intent_ids"] == [_intent(world)]
    # The previous band took 120 minutes a chapter: the window runs four such chapters past chapter 4's end.
    assert digest_calls[0]["as_of_chapter"] == 4
    assert digest_calls[0]["window_end_minute"] == OPENING + 4 * 120 + 4 * 120

    # A chapter that has a plan row gets it back without a model call, whatever its status.
    with Session.begin() as session:
        assert planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=6,
                                               policy=POLICY).id == plan_id
        assert planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=2,
                                               policy=POLICY).title == "old-2"
    assert len(client.calls) == 1
