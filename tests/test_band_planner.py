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
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
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
            target_total_chapters=12, runtime_policy=POLICY, creation_status="genesis_ready",
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


def _pending_conflict(Session, world: _World, *, flag: bool = True, refs=("delta-1",), intent: bool = True) -> str:
    """A world conflict resolved with plot_responds; ``flag`` marks it as waiting for a plan, ``intent`` names the
    intent besides the acting rival."""
    with Session.begin() as session:
        repo = WorldConflictRepository(session)
        row = repo.create(ConflictDraft(
            project_id=world.project_id, source="world_tick", detected_after_chapter=0, story_time=OPENING,
            involved_refs=[_intent(world), world.rival] if intent else [world.rival], violated="impact_review",
            impact_level="major", summary="conflict-author-summary", proposed_change={},
            intent_ref=_intent(world) if intent else "", actor_ref=world.rival, blocks_from_chapter=1,
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


def test_replan_keeps_accepted_rows_and_reapplies_active_patches(Session, digest_calls) -> None:
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
    # Two chapters are planned, but the digest window still runs to the band's last chapter.
    assert digest_calls[1]["window_end_minute"] == OPENING + 4 * DAY
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
    bound_id = _pending_conflict(Session, world, refs=("delta-2",))
    with Session.begin() as session:
        from forwin.world_engine.conflict_resolution import record_plot_response_patch
        plan = StateUpdater(session).create_chapter_plan(world.project_id, arc_id, 5, "later", "", [])
        repo = WorldConflictRepository(session)
        row = repo.get(world.project_id, bound_id)
        patch_id = record_plot_response_patch(session, row, plan)
        repo.attach_result_refs(row, [f"plan_patch:{patch_id}"])
    with Session() as session:
        assert [row.id for row in WorldConflictRepository(session).pending_plot_responses(world.project_id)] == [
            conflict_id,
        ]
    bound = {"world_bindings": [{"kind": "intent", "ref": _intent(world), "how": "answer the change"}]}
    client = StubLLM(_band(world, range(1, 5)), _band(world, range(1, 5), {2: bound}))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    (pending,) = client.payload(0)["pending_responses"]
    assert (pending["id"], pending["goal"]) == (conflict_id, PLOT_RESPONSE_GENERIC_GOAL)
    # The rival is an actor: no binding kind takes it, so only the intent is offered.
    assert pending["refs"] == [_intent(world)]
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
        assert any(item["kind"] == "intent" and item["ref"] == _intent(world)
                   for item in json.loads(plans[2].world_bindings_json))
        conflict = session.get(WorldPlanConflictRow, conflict_id)
        assert json.loads(conflict.result_refs_json) == ["delta-1", f"plan_patch:{patch.id}"]
        assert WorldConflictRepository(session).pending_plot_responses(world.project_id) == []


def test_plot_response_with_only_carrier_beyond_target_stays_pending(Session) -> None:
    from forwin.world_engine.conflict_resolution import record_plot_response_patch

    world, arc_id, _sheet_id = _book(Session)
    conflict_id = _pending_conflict(Session, world)
    with Session.begin() as session:
        target = session.get(Project, world.project_id).target_total_chapters
        plan = StateUpdater(session).create_chapter_plan(world.project_id, arc_id, target + 1, "later", "", [])
        repo = WorldConflictRepository(session)
        row = repo.get(world.project_id, conflict_id)
        patch_id = record_plot_response_patch(session, row, plan)
        repo.attach_result_refs(row, [f"plan_patch:{patch_id}"])
        plan_id = plan.id

    with Session() as session:
        repo = WorldConflictRepository(session)
        row = repo.get(world.project_id, conflict_id)
        (patch,) = repo.response_patches(row)
        assert (patch.id, patch.target_plan_id, patch.affected_chapters) == (patch_id, plan_id, [target + 1])
        plan = session.get(ChapterPlan, plan_id)
        assert plan.status == "planned"
        assert any(item["kind"] == "intent" and item["ref"] == _intent(world)
                   for item in json.loads(plan.world_bindings_json))
        assert repo.response_carriers(row) == []
        assert [pending.id for pending in repo.pending_plot_responses(world.project_id)] == [conflict_id]


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
        assert json.loads(plan.world_bindings_json) == []


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


@pytest.mark.parametrize("status", ["planned", "failed"])
def test_ensure_replans_a_legacy_row_in_place(Session, status) -> None:
    """A row the old planner wrote (no band plan) that is still to write is planned again (Ruling P4a-R27)."""
    world, arc_id, _sheet_id = _book(Session)
    with Session.begin() as session:
        updater = StateUpdater(session)
        written = updater.create_chapter_plan(world.project_id, arc_id, 1, "legacy-1", "", ["legacy-goal"])
        written.status = "accepted"
        legacy = updater.create_chapter_plan(world.project_id, arc_id, 2, "legacy-2", "", ["legacy-goal"])
        legacy.status = status
        written_id, legacy_id = written.id, legacy.id
    client = StubLLM(_band(world, range(2, 5)))
    planner = _planner(client)
    with Session.begin() as session:
        plan = planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=2, policy=POLICY)
        # Rewritten in place: same row and status, the band plan's content.
        assert (plan.id, plan.title, plan.status) == (legacy_id, "title-2", status)
        band_plan_id = plan.band_plan_id
        assert band_plan_id is not None

    # The accepted legacy row stays as it is; the band's rows to write are planned around it.
    assert client.payload(0)["chapters_to_plan"] == [2, 3, 4]
    with Session.begin() as session:
        plans = _plans(session, world.project_id)
        assert (plans[1].id, plans[1].title, plans[1].band_plan_id) == (written_id, "legacy-1", None)
        assert {number: plans[number].band_plan_id for number in (2, 3, 4)} == dict.fromkeys((2, 3, 4), band_plan_id)
        # Now band-planned, the row comes back without another model call.
        assert planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=2,
                                               policy=POLICY).id == legacy_id
    assert len(client.calls) == 1


def test_pending_response_with_no_bindable_ref_asks_for_its_goal(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    conflict_id = _pending_conflict(Session, world, intent=False)
    carried = {1: {"goals": [PLOT_RESPONSE_GENERIC_GOAL, "goal-1"]}}
    client = StubLLM(_band(world, range(1, 5)), _band(world, range(1, 5), carried))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    assert client.payload(0)["pending_responses"][0]["refs"] == []
    feedback = _feedback(client, 1)
    assert f"pending response {conflict_id} is carried by no chapter: copy its goal text into a chapter's goals" in feedback
    assert outcome.band_plan_id is not None


def test_failure_leaves_a_conflict_opened_during_the_model_call_alone(Session) -> None:
    from forwin.planning.band_planner.types import PlanIssue

    world, arc_id, _sheet_id = _book(Session)
    issue = PlanIssue(code="onstage_unavailable", severity="error", chapter_number=2, ref=world.rival, message="m")
    opened: list[str] = []

    def open_conflict() -> None:
        if not opened:
            with Session.begin() as other:
                opened.append(WorldConflictRepository(other).create(ConflictDraft(
                    project_id=world.project_id, source="band_planning", detected_after_chapter=0,
                    story_time=OPENING, involved_refs=[], violated="feasibility", impact_level="major",
                    summary="opened elsewhere", proposed_change={}, blocks_from_chapter=1,
                ), required_decider="human", options=("amend_plan",)).id)

    client = HookedLLM(open_conflict, _band(world, range(1, 5)), _band(world, range(1, 5)))
    with Session.begin() as session:
        assert _planner(client, StubAudit([issue], [issue])).ensure_plan_for_chapter(
            session, project_id=world.project_id, chapter_number=1, policy=POLICY,
        ) is None

    with Session() as session:
        assert [row.id for row in WorldConflictRepository(session).list(world.project_id)] == opened


@pytest.mark.parametrize("every_call", [False, True], ids=["replanned_once", "replanned_every_call"])
def test_ensure_plans_again_when_a_concurrent_plan_leaves_the_chapter_out(Session, every_call) -> None:
    world, arc_id, sheet_id = _book(Session)
    first = _plan_band(Session, _planner(StubLLM(_band(world, range(1, 5)))), world, arc_id, sheet_id)
    with Session.begin() as session:
        session.delete(_plans(session, world.project_id)[2])
    replans: list[str] = []

    def replan_from_three() -> None:
        if every_call or not replans:
            with Session.begin() as other:
                replans.append(_planner(StubLLM(_band(world, (3, 4)))).replan_remaining(
                    other, project_id=world.project_id, band_plan_id=first.band_plan_id, from_chapter=3,
                    policy=POLICY, notes=[],
                ).band_plan_id)

    client = HookedLLM(replan_from_three, _band(world, range(1, 5)), _band(world, range(1, 5)))
    with Session.begin() as session:
        plan = _planner(client).ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=2,
                                                        policy=POLICY)
        plan_id = plan.id if plan is not None else None

    # The first answer lost the band to the concurrent replan; ensure asked once more.
    assert len(client.calls) == 2
    with Session() as session:
        conflicts = WorldConflictRepository(session).list(world.project_id)
        traces = [json.loads(trace.output_summary_json) for trace in _traces(session, world.project_id)]
        if every_call:
            # Both answers lost: the chapter is still unplanned, so the band is blocked.
            assert plan_id is None and 2 not in _plans(session, world.project_id)
            (conflict,) = conflicts
            assert [item["code"] for item in json.loads(conflict.proposed_change_json)["issues"]] == [
                "chapter_unplanned",
            ]
        else:
            assert plan_id == _plans(session, world.project_id)[2].id and conflicts == []
        assert sum(trace["superseded_by_concurrent_plan"] for trace in traces) == (2 if every_call else 1)


def test_band_rows_stay_locked_from_the_re_read_until_the_plan_is_written(Session, monkeypatch) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from forwin.planning.band_planner import service as module

    world, arc_id, sheet_id = _book(Session)
    first = _plan_band(Session, _planner(StubLLM(_band(world, range(1, 5)))), world, arc_id, sheet_id)
    with Session() as session:
        third_id = _plans(session, world.project_id)[3].id
    attempts: list[str] = []
    real = module.persist_band_plan

    def persist_after_a_status_change(session, **kwargs):
        # A writer that does not take the project lock tries to move chapter 3 on before the plan is written.
        try:
            with Session.begin() as other:
                other.execute(text("SET LOCAL lock_timeout = '300ms'"))
                other.get(ChapterPlan, third_id).status = "needs_review"
            attempts.append("committed")
        except OperationalError:
            attempts.append("blocked")
        return real(session, **kwargs)

    monkeypatch.setattr(module, "persist_band_plan", persist_after_a_status_change)
    with Session.begin() as session:
        _planner(StubLLM(_band(world, (3, 4), {3: {"title": "title-3b"}}))).replan_remaining(
            session, project_id=world.project_id, band_plan_id=first.band_plan_id, from_chapter=3,
            policy=POLICY, notes=[],
        )

    assert attempts == ["blocked"]
    with Session() as session:
        third = session.get(ChapterPlan, third_id)
        assert (third.status, third.title) == ("planned", "title-3b")


def test_an_answered_attempt_keeps_its_issues_after_a_provider_failure(Session) -> None:
    from forwin.planning.band_planner.types import PlanIssue

    world, arc_id, _sheet_id = _book(Session)
    issue = PlanIssue(code="onstage_unavailable", severity="error", chapter_number=2, ref=world.rival, message="m")
    client = StubLLM(_band(world, range(1, 5)), RuntimeError("provider down"))
    with Session.begin() as session:
        assert _planner(client, StubAudit([issue])).ensure_plan_for_chapter(
            session, project_id=world.project_id, chapter_number=1, policy=POLICY,
        ) is None

    with Session() as session:
        (conflict,) = WorldConflictRepository(session).list(world.project_id)
        assert [item["code"] for item in json.loads(conflict.proposed_change_json)["issues"]] == [
            "onstage_unavailable", "llm_unavailable",
        ]
        (trace,) = _traces(session, world.project_id)
        assert json.loads(trace.output_summary_json)["failure_reason"] == "infeasible"


def test_a_raising_audit_saves_the_trace_before_the_error_surfaces(Session) -> None:
    from forwin.planning.band_planner.service import BandPlanner

    class RaisingAudit:
        def audit_plans(self, session, **kwargs):
            raise RuntimeError("audit broke")

    world, arc_id, sheet_id = _book(Session)
    saved: list[dict] = []

    def save_trace(session, prompt_trace, **kwargs):
        saved.append(prompt_trace)
        return save_world_prompt_trace(session, prompt_trace, **kwargs)

    client = StubLLM(_band(world, range(1, 5)), _band(world, range(1, 5)))
    planner = BandPlanner(client, feasibility=RaisingAudit(), save_trace=save_trace)
    with pytest.raises(RuntimeError, match="audit broke"):
        _plan_band(Session, planner, world, arc_id, sheet_id)

    # The error is never fed back: one model call, and its trace was saved before the error surfaced.
    assert len(client.calls) == 1
    (trace,) = saved
    assert (trace["stage_key"], trace["output_summary"]["failure_reason"]) == ("plan.band", "audit_error")
    assert len(trace["attempts"]) == 1


def test_a_book_without_a_story_calendar_is_not_planned(Session) -> None:
    from forwin.models.book_frame import StoryCalendarRow

    world, arc_id, sheet_id = _book(Session)
    with Session.begin() as session:
        session.execute(StoryCalendarRow.__table__.delete().where(StoryCalendarRow.project_id == world.project_id))
    client = StubLLM()
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    assert client.calls == []
    assert (outcome.band_plan_id, [issue.code for issue in outcome.issues]) == (None, ["no_story_calendar"])
    with Session() as session:
        (trace,) = _traces(session, world.project_id)
        assert json.loads(trace.output_summary_json)["failure_reason"] == "no_story_calendar"


def test_first_band_plan_over_a_written_chapter_lists_the_whole_band(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    with Session.begin() as session:
        plan = StateUpdater(session).create_chapter_plan(world.project_id, arc_id, 1, "old-1", "old-line", ["old-goal"])
        plan.status = "accepted"
    client = StubLLM(_band(world, (2, 3, 4)))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)

    assert client.payload(0)["chapters_to_plan"] == [2, 3, 4]
    with Session() as session:
        chapters = json.loads(session.get(BandPlanRow, outcome.band_plan_id).plan_json)["chapters"]
        assert [(item["chapter_number"], item["title"]) for item in chapters] == [
            (1, "old-1"), (2, "title-2"), (3, "title-3"), (4, "title-4"),
        ]
        assert (chapters[0]["one_line"], chapters[0]["goals"], chapters[0]["onstage_refs"]) == (
            "old-line", ["old-goal"], [],
        )


def test_replan_skips_chapter_with_pending_resume(Session) -> None:
    world, arc_id, sheet_id = _book(Session)
    client = StubLLM(
        _band(world, range(1, 5)),
        {"chapters": [_chapter(world, 2, title="title-2b"), _chapter(world, 4, title="title-4b")]},
        {"chapters": [_chapter(world, number, title=f"title-{number}c") for number in (2, 3, 4)]},
    )
    planner = _planner(client)
    outcome = _plan_band(Session, planner, world, arc_id, sheet_id)
    with Session.begin() as session:
        _plans(session, world.project_id)[1].status = "accepted"
        # Chapter 3 paused on a chapter conflict a human decided: it resumes from its saved draft.
        repo = WorldConflictRepository(session)
        row = repo.create(ConflictDraft(
            project_id=world.project_id, source="chapter_extraction", origin_key="chapter_extraction:3:protection-1",
            detected_after_chapter=2, story_time=OPENING, involved_refs=[world.hero], violated="protection",
            impact_level="major", summary="summary-chapter-conflict", proposed_change={}, blocks_from_chapter=3,
        ), required_decider="human", options=("rewrite_chapter", "amend_plan"))
        repo.mark_resolved(row, decision=ConflictDecision(
            option="rewrite_chapter", decided_by="human", rationale="rationale", decided_at=datetime.now(UTC),
        ), result_refs=[])
        conflict_id = row.id

    def replan():
        with Session.begin() as session:
            return planner.replan_remaining(
                session, project_id=world.project_id, band_plan_id=outcome.band_plan_id, from_chapter=2,
                policy=POLICY, notes=[],
            )

    replan()

    # The planned chapter waiting to resume keeps its plan.
    assert client.payload(1)["chapters_to_plan"] == [2, 4]
    with Session() as session:
        plans = _plans(session, world.project_id)
        assert [plans[number].title for number in (2, 3, 4)] == ["title-2b", "title-3", "title-4b"]
    # Once the resume took its candidate, the chapter is the planner's again.
    with Session.begin() as session:
        repo = WorldConflictRepository(session)
        repo.attach_result_refs(repo.get(world.project_id, conflict_id, for_update=True), ["candidate:candidate-3"])
    replan()
    assert client.payload(2)["chapters_to_plan"] == [2, 3, 4]


@pytest.mark.parametrize("undo", ["superseded_resolution", "conflict_not_resolved"])
def test_replan_does_not_reapply_a_patch_no_longer_in_force(Session, undo) -> None:
    world, arc_id, sheet_id = _book(Session)
    conflict_id = _pending_conflict(Session, world)
    first = _band(world, range(1, 5), {1: {"goals": [PLOT_RESPONSE_GENERIC_GOAL, "goal-1"]}})
    client = StubLLM(first, _band(world, range(1, 5), {1: {"goals": ["goal-1b"]}}))
    planner = _planner(client)
    outcome = _plan_band(Session, planner, world, arc_id, sheet_id)
    with Session.begin() as session:
        if undo == "superseded_resolution":
            supersede_resolutions(session, project_id=world.project_id, graph_delta_id="delta-1", reason="rewritten")
        else:
            session.get(WorldPlanConflictRow, conflict_id).status = "superseded"
    with Session.begin() as session:
        planner.replan_remaining(session, project_id=world.project_id, band_plan_id=outcome.band_plan_id,
                                 from_chapter=1, policy=POLICY, notes=[])

    with Session() as session:
        assert json.loads(_plans(session, world.project_id)[1].goals_json) == ["goal-1b"]


def test_concurrent_planners_write_one_band_plan(Session) -> None:
    import threading

    from forwin.planning.band_planner.types import PlanIssue

    world, arc_id, sheet_id = _book(Session)
    barrier = threading.Barrier(2, timeout=30)
    warning = PlanIssue(code="clock_unanswered", severity="warning", message="w")
    outcomes: list = []
    errors: list[BaseException] = []

    def plan() -> None:
        try:
            client = HookedLLM(barrier.wait, _band(world, range(1, 5)))
            outcomes.append(_plan_band(Session, _planner(client, StubAudit([warning])), world, arc_id, sheet_id))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=plan) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert errors == []
    with Session() as session:
        (band_plan,) = session.scalars(select(BandPlanRow).where(BandPlanRow.project_id == world.project_id))
        traces = [json.loads(trace.output_summary_json) for trace in _traces(session, world.project_id)]
    assert sorted(trace["superseded_by_concurrent_plan"] for trace in traces) == [False, True]
    winner, loser = sorted(outcomes, key=lambda outcome: bool(outcome.chapter_plan_ids), reverse=True)
    assert (winner.band_plan_id, [issue.code for issue in winner.issues]) == (band_plan.id, ["clock_unanswered"])
    # The loser's draft was discarded, so are its warnings; it reports the plan that stands.
    assert (loser.band_plan_id, loser.chapter_plan_ids, loser.issues) == (band_plan.id, [], [])


def test_ensure_plans_around_previously_drafted_old_row(Session):
    """Planning another chapter in the band must also preserve a reset historical plan."""
    world, arc_id, _ = _book(Session)
    with Session.begin() as session:
        plan = StateUpdater(session).create_chapter_plan(world.project_id, arc_id, 2, "original-2", "original-line", ["original-goal"])
        plan.status = "planned"
        session.add(ChapterDraft(chapter_plan_id=plan.id, body_text="previously written"))
        original = (plan.id, plan.title, plan.one_line, plan.goals_json)
    client = StubLLM(_band(world, (1, 3, 4)))
    with Session.begin() as session:
        assert _planner(client).ensure_plan_for_chapter(
            session, project_id=world.project_id, chapter_number=1, policy=POLICY,
        ).chapter_number == 1
    assert client.payload(0)["chapters_to_plan"] == [1, 3, 4]
    with Session() as session:
        plan = _plans(session, world.project_id)[2]
        assert (plan.id, plan.title, plan.one_line, plan.goals_json) == original
        assert plan.band_plan_id is None


def test_planner_rebinds_an_orphaned_response_and_retires_its_old_patch(Session):
    from forwin.world_engine.conflict_resolution import record_plot_response_patch
    from forwin.planning.band_planner.persistence import reapply_active_patches

    world, arc_id, sheet_id = _book(Session)
    conflict_id = _pending_conflict(Session, world)
    with Session.begin() as session:
        old = StateUpdater(session).create_chapter_plan(world.project_id, arc_id, 5, "old", "", ["old-goal"])
        repo = WorldConflictRepository(session)
        row = repo.get(world.project_id, conflict_id)
        old_patch = record_plot_response_patch(session, row, old)
        repo.attach_result_refs(row, [f"plan_patch:{old_patch}"])
        old_id = old.id
        old.status = "cancelled"
    bound = {"world_bindings": [{"kind": "intent", "ref": _intent(world), "how": "response"}]}
    client = StubLLM(_band(world, range(1, 5), {2: bound}))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)
    assert outcome.band_plan_id is not None
    assert [item["id"] for item in client.payload(0)["pending_responses"]] == [conflict_id]
    with Session.begin() as session:
        repo = WorldConflictRepository(session)
        row = repo.get(world.project_id, conflict_id)
        assert repo.pending_plot_responses(world.project_id) == []
        [patch] = repo.response_patches(row)
        assert patch.id != old_patch and patch.affected_chapters == [2]
        new = _plans(session, world.project_id)[2]
        new.world_bindings_json = "[]"
        assert patch.id in reapply_active_patches(session, new)
        assert [item["ref"] for item in json.loads(new.world_bindings_json)] == [_intent(world)]
        old = session.get(ChapterPlan, old_id)
        old.status, old.world_bindings_json = "planned", "[]"
        assert old_patch not in reapply_active_patches(session, old)
        assert json.loads(old.world_bindings_json) == []


def test_band_payload_respects_budget_and_matches_offered_refs():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SASession
    from forwin.models import Base
    from forwin.book_state.map_graph import MapGraph
    from forwin.protocol.book_state import FactNode, MapEdge
    from forwin.planning.band_planner.inputs import build_band_inputs
    from tests.test_world_tick_steps import _runtime, _actor, _intent

    nodes = [_actor("hero", location="place-000")]
    for index in range(120):
        nodes.extend([_actor(f"actor-{index:03}", location=f"place-{index:03}"),
                      _intent(f"intent-{index:03}", f"actor-{index:03}", at=0)])
    runtime = _runtime(*nodes)
    runtime.map = MapGraph(
        nodes=[MapNode(id=f"place-{i:03}", project_id=runtime.project_id, node_type="site",
                       name=f"Place {i} " + "x" * 200, subworld_id="band" if i < 3 else "outside")
               for i in range(121)],
        edges=[MapEdge(id=f"road-{i}", project_id=runtime.project_id, from_node_id="place-000",
                       to_node_id=f"place-{i:03}", edge_type="road", travel_time=i, bidirectional=True)
               for i in range(1, 120)],
    )
    for index in range(120):
        fact = FactNode(id=f"fact-{index:03}", project_id=runtime.project_id, proposition="truth " + "y" * 300,
                        related_node_refs=[f"actor-{index:03}"])
        runtime.world.facts_by_id[fact.id] = fact
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with SASession(engine) as session:
        arc = ArcPlanVersion(id="arc", project_id=runtime.project_id, chapter_start=4, chapter_end=7)
        band = BandSpan(band_index=0, chapter_start=4, chapter_end=7)
        sheet = ArcBeatSheetData(protagonist_ref="hero", phases=[], band_breakdown=[band])
        policy = POLICY.model_copy(update={"planning": POLICY.planning.model_copy(
            update={"band_payload_budget_chars": 7000},
        )})
        kwargs = dict(project_id=runtime.project_id, arc_plan=arc, beat_sheet=sheet, band=band,
                      chapter_numbers=[4, 5, 6, 7], rows={}, as_of=3, runtime=runtime,
                      policy=policy, notes=[], trope_cost_ceiling=3)
        result = build_band_inputs(session, **kwargs)
        payload = result.payload
        from forwin.writer.prompt_core.planning import build_band_plan_messages
        messages = build_band_plan_messages(payload=payload, digest_text=result.digest_text,
                                            output_schema={}, allowed={})
        prompt_payload, payload_end = json.JSONDecoder().raw_decode(messages[1]["content"])
        assert payload_end <= 7000
        assert prompt_payload == payload
        assert build_band_inputs(session, **kwargs).payload == payload
        assert payload["locations"][0]["id"] == "place-000"
        assert "place-120" not in result.offered.location_ids
        assert "hero" in result.offered.character_ids
        for field, offered in (("actors", result.offered.actor_ids), ("locations", result.offered.location_ids),
                               ("intents", result.offered.intent_ids), ("events", result.offered.event_ids),
                               ("clocks", result.offered.clock_ids), ("relations", result.offered.relation_ids),
                               ("milestones", result.offered.milestone_ids)):
            assert {item["id"] for item in payload[field]} == offered
        assert {item["ref"]: item["proposition"] for item in payload["facts"]} == result.offered.facts
        assert {item["owner_ref"] for item in payload["intents"]} <= result.offered.actor_ids
        assert all(set(runtime.world.facts_by_id[item["ref"][5:]].related_node_refs) & result.offered.actor_ids
                   for item in payload["facts"])

        from forwin.planning.band_planner.types import BandPlanOutput, ChapterPlanDraft
        from forwin.planning.band_planner.validation import band_plan_issues
        chapter = ChapterPlanDraft(
            chapter_number=4, title="title", one_line="summary", goals=["goal"], pov_ref="hero",
            onstage_refs=sorted(result.offered.actor_ids), location_refs=sorted(result.offered.location_ids),
            expected_span={"amount": 1, "unit": "minute"},
            world_bindings=[{"kind": kind, "ref": ref} for kind in ("intent", "secret", "clock", "event", "relation", "news")
                            for ref in sorted(result.offered.binding_refs(kind))],
        )
        def ref_errors(item):
            return {(issue.code, issue.ref) for issue in band_plan_issues(
                BandPlanOutput(chapters=[item]), chapter_numbers=[4], offered=result.offered,
                runtime=runtime, calendar=result.calendar, lexicon=result.lexicon,
            ) if issue.code in {"unknown_actor", "unknown_location", "unknown_binding_ref", "unknown_milestone"}}
        assert ref_errors(chapter) == set()
        bad = chapter.model_copy(update={"onstage_refs": ["hero", "actor-119"], "location_refs": ["place-120"]})
        assert ref_errors(bad) == {("unknown_actor", "actor-119"), ("unknown_location", "place-120")}
        too_small = policy.model_copy(update={"planning": policy.planning.model_copy(
            update={"band_payload_budget_chars": 1},
        )})
        with pytest.raises(ValueError, match="budget"):
            build_band_inputs(session, **dict(kwargs, policy=too_small))
    engine.dispose()


@pytest.mark.parametrize("requirement", ["reader_reveal", "protection_fact", "protection_ref"])
def test_band_payload_offers_node_facts_and_keeps_required_facts_under_budget(requirement):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SASession
    from forwin.models import Base
    from forwin.protocol.book_state import FactNode
    from forwin.planning.band_planner.inputs import build_band_inputs
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor, _intent

    runtime = _runtime(
        _actor("hero", location="site-1"), _actor("remote", location="remote-site"),
        WorldNode(id="society", project_id=PROJECT, node_type="institution", state={"location_id": "site-1"}),
        WorldNode(id="group", project_id=PROJECT, node_type="group", state={"location_id": "site-1"}),
        WorldNode(id="event", project_id=PROJECT, node_type="event", created_at_chapter=3),
        WorldNode(id="clock", project_id=PROJECT, node_type="clock", state={"status": "running"}),
        _intent("intent", "hero", at=0),
    )
    for node in runtime.map.nodes_by_id.values():
        node.subworld_id = "band-subworld"
    facts = [FactNode(id=f"about-{ref}", project_id=PROJECT, proposition=f"truth about {ref}",
                      related_node_refs=[ref]) for ref in ("society", "site-1", "group", "event", "clock", "intent")]
    facts += [FactNode(id=f"optional-{i:03}", project_id=PROJECT, proposition="optional " + "x" * 250,
                       related_node_refs=["hero"]) for i in range(30)]
    facts += [FactNode(id="required", project_id=PROJECT, proposition="required truth", fact_type="event",
                       related_node_refs=["remote"], created_at_chapter=0),
              FactNode(id="unrelated", project_id=PROJECT, proposition="remote truth", related_node_refs=["remote"]),
              FactNode(id="global", project_id=PROJECT, proposition="unscoped truth")]
    facts += [FactNode(id=ref, project_id=PROJECT, proposition=ref, related_node_refs=["remote"])
              for ref in ("past", "future", "actor-reveal", "expired-protection")]
    # A required event fact must also survive the ordinary recent-event selection.
    facts += [FactNode(id=f"recent-{i:03}", project_id=PROJECT, proposition="recent truth", fact_type="event",
                       related_node_refs=["remote"], created_at_chapter=3) for i in range(30)]
    runtime.world.facts_by_id.update({fact.id: fact for fact in facts})
    band = BandSpan(band_index=0, chapter_start=4, chapter_end=7)
    extra = {"reveal_targets": [{"fact_ref": "fact:required", "observer": "reader",
                                  "target_state": "knows", "range": [4, 4]}]} if requirement == "reader_reveal" else {
        "protections": [{"ref": "hero", "kind": "must_not_know", "observer_ref": "hero",
                         "fact_ref": "fact:required", "until_chapter": 4}] if requirement == "protection_fact" else [
            {"ref": "fact:required", "kind": "must_exist", "until_chapter": 4}],
    }
    sheet = ArcBeatSheetData.model_validate({"protagonist_ref": "hero", "phases": [],
                                           "band_breakdown": [band], **extra})
    sheet = ArcBeatSheetData.model_validate({
        **sheet.model_dump(),
        "reveal_targets": [*sheet.reveal_targets,
                           {"fact_ref": "fact:past", "observer": "reader", "target_state": "knows", "range": [1, 3]},
                           {"fact_ref": "fact:future", "observer": "reader", "target_state": "knows", "range": [8, 9]},
                           {"fact_ref": "fact:actor-reveal", "observer": "hero", "target_state": "knows", "range": [4, 7]}],
        "protections": [*sheet.protections, {"ref": "hero", "kind": "must_not_know", "observer_ref": "hero",
                                             "fact_ref": "fact:expired-protection", "until_chapter": 3}],
    })
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with SASession(engine) as session:
        kwargs = dict(project_id=PROJECT, arc_plan=ArcPlanVersion(id="arc", project_id=PROJECT,
                      chapter_start=4, chapter_end=7), beat_sheet=sheet, band=band, chapter_numbers=[7],
                      rows={}, as_of=3, runtime=runtime, notes=[], trope_cost_ceiling=3)
        full = build_band_inputs(session, **kwargs, policy=POLICY)
        assert "society" in {item["id"] for item in full.payload["world_nodes"]}
        assert "fact:about-society" in full.offered.facts
        assert {f"fact:about-{ref}" for ref in ("site-1", "group", "event", "clock", "intent")} <= full.offered.facts.keys()
        assert "fact:global" in full.offered.facts
        assert "fact:unrelated" not in full.offered.facts
        assert not {"fact:past", "fact:future", "fact:expired-protection"} & full.offered.facts.keys()
        assert "fact:actor-reveal" in full.offered.facts
        assert "fact:required" in full.offered.facts
        policy = POLICY.model_copy(update={"planning": POLICY.planning.model_copy(
            update={"band_payload_budget_chars": 3500},
        )})
        trimmed = build_band_inputs(session, **kwargs, policy=policy)
        assert len(json.dumps(full.payload, ensure_ascii=False)) > 3500
        assert len(json.dumps(trimmed.payload, ensure_ascii=False)) <= 3500
        assert "fact:required" in trimmed.offered.facts
        assert "fact:actor-reveal" in trimmed.offered.facts
        assert len(trimmed.offered.facts) < len(full.offered.facts)
        assert trimmed.offered.facts == {item["ref"]: item["proposition"] for item in trimmed.payload["facts"]}
        offered_nodes = {item["id"] for key in ("actors", "locations", "world_nodes", "intents", "events", "clocks")
                         for item in trimmed.payload[key]}
        assert all(item["ref"] in {"fact:required", "fact:actor-reveal"} or not item["related_refs"]
                   or offered_nodes.intersection(item["related_refs"]) for item in trimmed.payload["facts"])
        assert build_band_inputs(session, **kwargs, policy=policy).payload == trimmed.payload
    engine.dispose()


def test_required_band_payload_over_budget_returns_failure_and_blocks_before_writing():
    from sqlalchemy import create_engine
    from forwin.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = get_session_factory(engine)
    world, arc_id, _ = _book(sessions)
    policy = POLICY.model_copy(update={"planning": POLICY.planning.model_copy(
        update={"band_payload_budget_chars": 1},
    )})
    client = StubLLM()
    planner = _planner(client)
    with sessions.begin() as session:
        outcome = planner.plan_band(
            session, project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id),
            beat_sheet_row=ArcBeatSheetRepository(session).active_for_arc(arc_id), policy=policy,
            band=BandSpan(band_index=0, chapter_start=1, chapter_end=4),
        )
        assert outcome.failure_reason == "payload_budget_exceeded"
        assert outcome.band_plan_id is None and outcome.chapter_plan_ids == []
        assert [issue.code for issue in outcome.issues] == ["payload_budget_exceeded"]
        assert WorldConflictRepository(session).list(world.project_id) == []
        assert planner.ensure_plan_for_chapter(session, project_id=world.project_id,
                                               chapter_number=1, policy=policy) is None
        (conflict,) = WorldConflictRepository(session).list(world.project_id)
        assert (conflict.source, conflict.status, conflict.required_decider, conflict.blocks_from_chapter) == (
            "band_planning", "open", "human", 1,
        )
        assert _plans(session, world.project_id) == {}
        assert list(session.scalars(select(BandPlanRow))) == []
        traces = _traces(session, world.project_id)
        assert len(traces) == 2
        assert all(json.loads(trace.output_summary_json)["failure_reason"] == "payload_budget_exceeded"
                   for trace in traces)
        assert all(json.loads(trace.output_summary_json)["band_payload_trim"]["overflow"] for trace in traces)
        assert all(json.loads(trace.output_summary_json)["band_payload_trim"]["before_chars"] > 1 for trace in traces)
        assert client.calls == []
    engine.dispose()


@pytest.fixture
def band_input_builder():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SASession
    from forwin.models import Base
    from forwin.planning.band_planner.inputs import build_band_inputs

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with SASession(engine) as session:
        def build(runtime, *, cap=60000, sheet=None):
            band = BandSpan(band_index=0, chapter_start=4, chapter_end=7)
            return build_band_inputs(
                session, project_id=runtime.project_id,
                arc_plan=ArcPlanVersion(id="arc", project_id=runtime.project_id, chapter_start=4, chapter_end=7),
                beat_sheet=sheet or ArcBeatSheetData(protagonist_ref="hero", phases=[], band_breakdown=[band]),
                band=band, chapter_numbers=[4, 5, 6, 7], rows={}, as_of=3, runtime=runtime,
                policy=POLICY.model_copy(update={"planning": POLICY.planning.model_copy(
                    update={"band_payload_budget_chars": cap})}), notes=[], trope_cost_ceiling=3,
            )
        yield build
    engine.dispose()


@pytest.mark.parametrize("observer", ["hero", "remote"])
def test_band_payload_offers_remote_news_and_facts(band_input_builder, monkeypatch, observer):
    from forwin.protocol.book_state import FactNode
    from forwin.world_engine.types import NewsItem
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor
    from forwin.planning.band_planner import inputs

    runtime = _runtime(_actor("hero", location="site-1"), _actor("remote", location="remote-site"))
    runtime.world.facts_by_id.update({
        "remote-news": FactNode(id="remote-news", project_id=PROJECT, proposition="remote event",
                                fact_type="event", related_node_refs=["old-event", "remote"], created_at_chapter=0),
        **{f"new-{i}": FactNode(id=f"new-{i}", project_id=PROJECT, proposition="new event", fact_type="event",
                               related_node_refs=["remote"], created_at_chapter=3) for i in range(30)},
    })
    monkeypatch.setattr(inputs, "latest_news_in_transit", lambda *args: [
        NewsItem(fact_ref="fact:remote-news", observer_ref=observer, arrive_at=10),
    ])
    result = band_input_builder(runtime)
    assert result.offered.news_fact_refs == {"fact:remote-news"}
    assert "fact:remote-news" in result.offered.facts
    assert observer in result.offered.actor_ids
    assert {item["fact_ref"] for item in result.payload["news"]} == result.offered.news_fact_refs
    assert {item["ref"] for item in result.payload["facts"]} == set(result.offered.facts)


def test_band_payload_small_overflow_keeps_clock_and_recent_event(band_input_builder):
    from forwin.protocol.book_state import FactNode
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor

    runtime = _runtime(
        _actor("hero", location="site-1"),
        WorldNode(id="clock", project_id=PROJECT, node_type="clock", state={"status": "running", "due_at": 20}),
        WorldNode(id="recent", project_id=PROJECT, node_type="event", created_at_chapter=3),
        WorldNode(id="older", project_id=PROJECT, node_type="event", created_at_chapter=1),
    )
    runtime.world.facts_by_id.update({
        f"optional-{i:02}": FactNode(id=f"optional-{i:02}", project_id=PROJECT, proposition="optional " + "x" * 150,
                                    related_node_refs=["hero"], created_at_chapter=i % 4) for i in range(30)
    })
    full = band_input_builder(runtime)
    cap = len(json.dumps(full.payload, ensure_ascii=False)) - 100
    trimmed = band_input_builder(runtime, cap=cap)
    assert len(json.dumps(trimmed.payload, ensure_ascii=False)) <= cap
    assert trimmed.trim_record["before_chars"] == len(json.dumps(full.payload, ensure_ascii=False))
    assert trimmed.trim_record["after_chars"] == len(json.dumps(trimmed.payload, ensure_ascii=False))
    assert trimmed.trim_record["removed"] == {"facts": 1}
    assert trimmed.trim_record["budget_chars"] == cap
    assert trimmed.offered.clock_ids == {"clock"}
    assert "recent" in trimmed.offered.event_ids
    removed = set(full.offered.facts) - set(trimmed.offered.facts)
    assert len(removed) == 1
    assert runtime.world.facts_by_id[next(iter(removed))[5:]].created_at_chapter == 0
    assert band_input_builder(runtime, cap=cap).payload == trimmed.payload


@pytest.mark.parametrize("status, scheduled_at", [("due", None), ("planned", 0), ("planned", 4 * DAY), ("held", 4 * DAY)])
def test_band_payload_keeps_due_onstage_intents_and_clocks_under_tight_budget(
    band_input_builder, status, scheduled_at,
):
    from forwin.protocol.book_state import FactNode
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor, _intent, _clock

    runtime = _runtime(
        _actor("hero", location="site-1"), _actor("remote", location="site-5"),
        _intent("required-intent", "remote", at=scheduled_at, status=status, onstage=True),
        _intent("later-intent", "hero", at=4 * DAY + 1, onstage=True),
        _intent("offstage-intent", "hero", at=0),
        _clock("due-clock", due_at=0), _clock("end-clock", due_at=4 * DAY),
        _clock("overdue-clock", due_at=-1), _clock("later-clock", due_at=4 * DAY + 1),
    )
    for node in runtime.map.nodes_by_id.values():
        node.subworld_id = "outside" if node.id == "site-5" else "band"
    runtime.world.facts_by_id.update({
        f"optional-{i}": FactNode(id=f"optional-{i}", project_id=PROJECT, proposition="x" * 300,
                                 related_node_refs=["hero"]) for i in range(20)
    })
    full = band_input_builder(runtime)
    trimmed = band_input_builder(runtime, cap=2600)
    assert len(json.dumps(full.payload, ensure_ascii=False)) > 2600
    assert len(json.dumps(trimmed.payload, ensure_ascii=False)) <= 2600
    for result in (full, trimmed):
        assert result.payload["due_onstage_intent_ids"] == ["required-intent"]
        assert "required-intent" in result.offered.intent_ids
        assert {"due-clock", "end-clock", "overdue-clock"} <= result.offered.clock_ids
        assert "remote" in result.offered.actor_ids
        assert result.offered.intent_ids == {item["id"] for item in result.payload["intents"]}
        assert result.offered.clock_ids == {item["id"] for item in result.payload["clocks"]}
        assert {item["owner_ref"] for item in result.payload["intents"]} <= result.offered.actor_ids
    assert len(trimmed.offered.facts) < len(full.offered.facts)
    assert "later-intent" not in trimmed.offered.intent_ids
    assert "later-clock" not in trimmed.offered.clock_ids
    assert band_input_builder(runtime, cap=2600).payload == trimmed.payload


def test_band_payload_protects_singleton_category_while_others_have_optional_tails(band_input_builder):
    from forwin.protocol.book_state import FactNode
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor

    runtime = _runtime(
        _actor("hero", location="site-1"),
        *[WorldNode(id=f"event-{i}", project_id=PROJECT, node_type="event", created_at_chapter=i)
          for i in (2, 3)],
    )
    runtime.world.facts_by_id.update({
        f"optional-{i}": FactNode(id=f"optional-{i}", project_id=PROJECT, proposition="x" * 100,
                                 related_node_refs=["hero"]) for i in range(5)
    })
    full = band_input_builder(runtime)
    # Two rounds shed two facts and two locations, but only the worse of the two events.
    removed_size = sum(len(json.dumps(item, ensure_ascii=False)) + 2
                       for key, count in (("facts", 2), ("events", 1), ("locations", 2))
                       for item in full.payload[key][-count:])
    cap = len(json.dumps(full.payload, ensure_ascii=False)) - removed_size
    trimmed = band_input_builder(runtime, cap=cap)
    assert trimmed.offered.event_ids == {"event-3"}
    assert len(trimmed.offered.facts) == 3
    assert len(trimmed.offered.location_ids - {"site-1"}) == 2
    assert len(json.dumps(trimmed.payload, ensure_ascii=False)) == cap


@pytest.mark.parametrize("far_chapter", [2, 3])
def test_band_payload_fact_locality_outweighs_tied_or_newer_recency(band_input_builder, far_chapter):
    from forwin.protocol.book_state import FactNode
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor

    runtime = _runtime(_actor("hero", location="site-1"), _actor("near", location="site-1"),
                       _actor("far", location="site-2"), edges=[(1, 2, 2)])
    runtime.world.facts_by_id.update({
        "z-near": FactNode(id="z-near", project_id=PROJECT, proposition="near fact",
                           related_node_refs=["near"], created_at_chapter=2),
        "a-far": FactNode(id="a-far", project_id=PROJECT, proposition="far fact",
                          related_node_refs=["far"], created_at_chapter=far_chapter),
    })
    full = band_input_builder(runtime)
    assert set(full.offered.facts) == {"fact:z-near", "fact:a-far"}
    cap = len(json.dumps(full.payload, ensure_ascii=False)) - 1
    trimmed = band_input_builder(runtime, cap=cap)
    assert set(trimmed.offered.facts) == {"fact:z-near"}
    assert len(json.dumps(trimmed.payload, ensure_ascii=False)) <= cap


def test_band_payload_missing_location_concealment_and_overdue_milestone(band_input_builder):
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor

    runtime = _runtime(_actor("hero"), WorldNode(
        id="society", project_id=PROJECT, node_type="institution", name="hidden society",
        state={"visibility_level": "hidden"},
    ))
    for node in runtime.map.nodes_by_id.values():
        node.subworld_id = node.id
    band = BandSpan(band_index=0, chapter_start=4, chapter_end=7)
    sheet = ArcBeatSheetData.model_validate({
        "protagonist_ref": "hero", "phases": [], "band_breakdown": [band],
        "milestones": [{"id": "overdue", "description": "overdue", "target_range": [-2, -1], "hard": True}],
    })
    result = band_input_builder(runtime, sheet=sheet)
    assert result.offered.location_ids == set(runtime.map.nodes_by_id)
    assert result.payload["world_nodes"][0]["concealed"] is True
    assert "overdue" in result.offered.milestone_ids


def test_band_payload_round_robin_ranks_categories_and_preserves_news_refs(band_input_builder, monkeypatch, caplog):
    import logging
    from types import SimpleNamespace
    from forwin.protocol.book_state import FactNode, WorldEdge
    from forwin.world_engine.types import NewsItem
    from forwin.planning.band_planner import inputs
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor, _intent

    runtime = _runtime(
        _actor("hero", location="site-1"), _actor("near", location="site-1"), _actor("far", location="site-2"),
        *[WorldNode(id=f"event-{i}", project_id=PROJECT, node_type="event", created_at_chapter=i,
                    profile={"location_id": "site-1"}) for i in range(1, 4)],
        *[WorldNode(id=f"clock-{i}", project_id=PROJECT, node_type="clock",
                    state={"status": "running", "due_at": 4 * DAY + i * 10}) for i in range(1, 4)],
        *[_intent(f"intent-{i}", "hero", at=i * 10) for i in range(1, 4)],
    )
    for i in range(1, 4):
        runtime.world.facts_by_id[f"optional-{i}"] = FactNode(
            id=f"optional-{i}", project_id=PROJECT, proposition="optional " + "x" * 200,
            related_node_refs=["hero"], created_at_chapter=i,
        )
        runtime.world.facts_by_id[f"news-{i}"] = FactNode(
            id=f"news-{i}", project_id=PROJECT, proposition="remote news", related_node_refs=["absent"],
        )
    for name, source, target in (("best", "hero", "near"), ("middle", "hero", "far"), ("worst", "near", "far")):
        edge = WorldEdge(id=name, project_id=PROJECT, source_id=source, target_id=target,
                         edge_type="ally_of", edge_family="social")
        runtime.world.edges_by_id[edge.id] = edge
    monkeypatch.setattr(inputs, "latest_news_in_transit", lambda *args: [
        NewsItem(fact_ref=f"fact:news-{i}", observer_ref="hero", arrive_at=i * 10) for i in range(1, 4)
    ])
    monkeypatch.setattr(inputs, "_stalled_threads", lambda *args, **kwargs: [
        {"name": f"thread-{i}", "description": "thread", "last_active_chapter": i, "priority": i}
        for i in range(1, 4)
    ])
    monkeypatch.setattr(inputs.WorldConflictRepository, "pending_plot_responses", lambda *args: [
        SimpleNamespace(id="pending", involved_refs_json='["event-1", "clock-3", "fact:news-3"]',
                        intent_ref="intent-3", actor_ref="hero", summary="pending summary"),
    ])
    monkeypatch.setattr(inputs, "writer_response_text", lambda row: "response goal")
    full = band_input_builder(runtime)
    expected = {"facts": "fact:optional-1", "events": "event-1", "clocks": "clock-3",
                "relations": "worst", "news": "fact:news-3", "stalled_threads": "thread-3"}
    fields = {"facts": "ref", "news": "fact_ref", "stalled_threads": "name"}
    removed_size = sum(len(json.dumps(next(item for item in full.payload[key]
                                           if item[fields.get(key, "id")] == ref), ensure_ascii=False)) + 2
                       for key, ref in expected.items())
    # Pending refs to the removed event and clock also disappear.
    removed_size += len(json.dumps("event-1")) + len(json.dumps("clock-3")) + 4
    cap = len(json.dumps(full.payload, ensure_ascii=False)) - removed_size
    with caplog.at_level(logging.INFO, logger=inputs.__name__):
        trimmed = band_input_builder(runtime, cap=cap)
    assert len(json.dumps(trimmed.payload, ensure_ascii=False)) == cap
    for key, ref in expected.items():
        field = fields.get(key, "id")
        assert {item[field] for item in full.payload[key]} - {item[field] for item in trimmed.payload[key]} == {ref}
    assert trimmed.offered.news_fact_refs <= trimmed.offered.facts.keys()
    assert trimmed.offered.pending[0].refs == frozenset(trimmed.payload["pending_responses"][0]["refs"])
    assert trimmed.offered.pending[0].refs == {"fact:news-3", "intent-3"}
    record = next(record for record in caplog.records if record.msg.startswith("Band payload trimmed:"))
    assert record.args == (len(json.dumps(full.payload, ensure_ascii=False)), cap, cap, dict.fromkeys(expected, 1))
    # Continue the same round through locations and intents, retaining their nearest/soonest items.
    expected.update({"locations": "site-5", "intents": "intent-3"})
    tail_size = sum(len(json.dumps(next(item for item in full.payload[key] if item["id"] == expected[key]),
                                   ensure_ascii=False)) + 2 for key in ("locations", "intents"))
    tail_size += len(json.dumps("intent-3")) + 2
    whole_round = band_input_builder(runtime, cap=cap - tail_size)
    assert len(json.dumps(whole_round.payload, ensure_ascii=False)) == cap - tail_size
    for key, ref in expected.items():
        field = fields.get(key, "id")
        assert {item[field] for item in full.payload[key]} - {item[field] for item in whole_round.payload[key]} == {ref}
    # A deeper cut keeps singleton categories while other categories still have a lower-ranked tail.
    for reduction in (200, 600, 1000):
        smaller = band_input_builder(runtime, cap=cap - reduction)
        assert len(json.dumps(smaller.payload, ensure_ascii=False)) <= cap - reduction
        assert smaller.offered.news_fact_refs <= smaller.offered.facts.keys()
        assert all(item["observer_ref"] in smaller.offered.actor_ids for item in smaller.payload["news"])
        assert all(set(item["related_refs"]) <= {
            item["id"] for key in ("actors", "locations", "world_nodes", "intents", "events", "clocks")
            for item in smaller.payload[key]
        } for item in smaller.payload["facts"])


def test_band_payload_keeps_remote_observer_and_protected_fact(band_input_builder):
    from forwin.protocol.book_state import FactNode
    from tests.test_world_tick_steps import PROJECT, _runtime, _actor

    runtime = _runtime(_actor("hero", location="site-1"), _actor("observer", location="unmapped"))
    runtime.world.facts_by_id.update({
        ref: FactNode(id=ref, project_id=PROJECT, proposition=ref, related_node_refs=["absent"])
        for ref in ("reveal", "protected")
    })
    runtime.world.facts_by_id.update({
        f"optional-{i}": FactNode(id=f"optional-{i}", project_id=PROJECT, proposition="x" * 300,
                                 related_node_refs=["hero"]) for i in range(20)
    })
    band = BandSpan(band_index=0, chapter_start=4, chapter_end=7)
    sheet = ArcBeatSheetData.model_validate({
        "protagonist_ref": "hero", "phases": [], "band_breakdown": [band],
        "reveal_targets": [{"fact_ref": "fact:reveal", "observer": "observer", "target_state": "knows", "range": [4, 7]}],
        "protections": [{"ref": "observer", "kind": "must_not_know", "observer_ref": "observer",
                         "fact_ref": "fact:protected", "until_chapter": 7}],
    })
    result = band_input_builder(runtime, sheet=sheet, cap=3000)
    assert len(json.dumps(result.payload, ensure_ascii=False)) <= 3000
    assert {"fact:reveal", "fact:protected"} <= result.offered.facts.keys()
    assert "observer" in result.offered.actor_ids
    assert all(item["location_ref"] in result.offered.location_ids | {""} for item in result.payload["actors"])
    assert all(set(item["related_refs"]) <= result.offered.actor_ids for item in result.payload["facts"])


def test_successful_band_trace_records_payload_trim(monkeypatch):
    from sqlalchemy import create_engine
    from forwin.models import Base
    from forwin.planning.band_planner import service

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = get_session_factory(engine)
    world, arc_id, sheet_id = _book(sessions)
    build = service.build_band_inputs
    captured = []

    def inputs(*args, **kwargs):
        result = build(*args, **kwargs)
        captured.append(result.trim_record)
        return result

    monkeypatch.setattr(service, "build_band_inputs", inputs)
    client = StubLLM(_band(world, range(1, 5)))
    result = _plan_band(sessions, _planner(client, StubAudit()), world, arc_id, sheet_id)
    assert result.band_plan_id is not None
    with sessions() as session:
        (trace,) = _traces(session, world.project_id)
        assert json.loads(trace.output_summary_json)["band_payload_trim"] == captured[0]
        assert captured[0]["before_chars"] >= captured[0]["after_chars"] > 0
    engine.dispose()
