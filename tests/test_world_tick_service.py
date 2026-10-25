from __future__ import annotations

import json
from functools import partial
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from forwin.audit.events import DecisionEventType
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.runtime import BookStateRuntime
from forwin.canon import CanonAdmissionService, CanonStaleVersion, CanonWorldTickOutcome
from forwin.canon.historical_rewrite import HistoricalCanonRewriteRepository
from forwin.maintenance.events import POST_CANON_STEP_NAMES
from forwin.maintenance.post_canon import (
    PostCanonMaintenanceService,
    own_transaction_audit_writer,
    save_world_prompt_trace,
)
from forwin.models import ArcPlanVersion, ChapterPlan, NarrativeObligationRow, Project
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import GraphDeltaRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.genesis import PromptTrace
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.goals import load_goals_json
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NarrativePatch, NodePatch, WorldNode
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.world_engine.actor_turns import ActorTurnRunner
from forwin.world_engine.adjudicator import AdjudicationCall, WorldAdjudicator
from forwin.world_engine.conflict_delegate import WorldConflictDelegate
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.delta_builder import TickIssue, WorldTickDeltaBuilder, tick_event_fact_ref
from forwin.world_engine import running_state
from forwin.world_engine.propagation import chapter_events
from forwin.world_engine.tick import WorldTickResult, WorldTickService
from forwin.world_engine.tick_nodes import outcome_candidate
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE, AdjudicatedOutcome, TickCandidate, WorldEffects
from forwin.writer.prompt_core.world_engine import ACTOR_CONSTRAINT_FEEDBACK, plot_response_goal
from tests import world_gate_helpers
from tests.test_canon_atomic_transaction import _prepare_chapter_candidate
from tests.test_historical_rewrite_world_ticks import _accept, _complete_post_canon
from tests.test_world_gate_v6 import (
    OPENING,
    _commit,
    _delta,
    _event,
    _seed_world,
    _session_factory,
    _time,
    _World,
)
from tests.test_world_tick_delta import _built, _commit_tick, _context


ASTRA = "gpt-6-astra"
POLICY = RuntimePolicy.for_profile("standard")
DAY = 1440
_STAGES = {"world.adjudicate": "adjudicate", "world.actor_turn": "actor_turn", "world.drift": "drift", "review.world_conflict": "review"}


def _policy(**world: Any) -> RuntimePolicy:
    return POLICY.model_copy(update={"world": POLICY.world.model_copy(update=world)})


# a stub model client for the three world roles


Handler = Callable[[dict[str, Any], str], dict[str, Any] | str]


class _WorldLLM:
    """Answers each world stage from its handler; a stage without one is unavailable."""

    model = "stub-world-model"

    def __init__(self, **handlers: Handler) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, str]] = []
        self.llm_attempt_events: list[dict[str, Any]] = []
        self.last_call_result = None

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        stage = str(kwargs.get("stage_key") or "")
        content = messages[-1]["content"]
        self.calls.append((stage, content))
        codex = stage == "review.world_conflict"
        model = ASTRA if codex else self.model
        backend = "codex_bridge" if codex else "stub"
        self.last_call_result = SimpleNamespace(
            backend=backend, fallback_used=False, actual_model=model, trace={"backend": backend, "actual_model": model},
        )
        handler = self.handlers.get(_STAGES[stage])
        if handler is None:
            raise ConnectionError(f"{stage} is unavailable")
        # A handler answers with an object, or with the raw text to return.
        reply = handler(json.JSONDecoder().raw_decode(content)[0], content)
        answer = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        self.llm_attempt_events.append({
            "attempt_no": 1, "stage_key": stage, "model": model, "output_chars": len(answer),
        })
        return answer

    def drain_llm_attempt_events(self):
        events, self.llm_attempt_events = self.llm_attempt_events, []
        return events

    def stages(self) -> list[str]:
        return [stage for stage, _content in self.calls]


def _turn(actor_id: str, *intents: dict[str, Any]) -> dict[str, Any]:
    return {"actor_id": actor_id, "new_intents": list(intents), "agenda_changes": [],
            "next_turn_in": {"amount": 1, "unit": "日"}}


def _turns(**turns: dict[str, Any]) -> Handler:
    """Every asked actor waits a day, except the ones given their own turn."""
    def answer(payload: dict[str, Any], _content: str) -> dict[str, Any]:
        return {"turns": [turns.get(actor["actor_id"], _turn(actor["actor_id"])) for actor in payload["actors"]]}

    return answer


def _quiet_turns(payload: dict[str, Any], content: str) -> dict[str, Any]:
    return _turns()(payload, content)


def _outcomes(*outcomes: dict[str, Any]) -> Handler:
    def answer(payload: dict[str, Any], _content: str) -> dict[str, Any]:
        asked = {intent["id"] for intent in payload["intents"]}
        return {"outcomes": [outcome for outcome in outcomes if outcome["intent_id"] in asked]}

    return answer


def _review(option: str) -> Handler:
    return lambda _payload, _content: {
        "option": option, "rationale": "rationale-review", "risk_level": "low", "evidence": [],
    }


# the world: the gate world's three actors plus a hero agenda, two intents and a public clock


def _seed_tick_world(
    Session, monkeypatch: pytest.MonkeyPatch, *, later_impact: str = "moderate", rival_strikes: bool = False,
    errands: int = 0,
) -> _World:
    """An onstage intent due an hour in, an offstage one three days in (``later_impact``), a clock two hours in.

    With ``rival_strikes`` the rival also means to strike at the hero three days in, offstage. ``errands``
    more offstage intents of the hero fall due one an hour from the first hour on.
    """
    original = world_gate_helpers._pack

    def pack(**kwargs: Any) -> dict[str, Any]:
        built = original(**kwargs)
        seed = built["world_seed"]
        intent = {
            "agenda_id": "agenda-1", "action_type": "confront", "target_refs": ["opp-1"], "location_ref": "node-1",
        }
        seed["actors"][0]["agendas"] = [{
            "id": "agenda-1", "goal": "goal-1", "motivation": "", "horizon": "", "priority": 3,
            "means_refs": [], "constraints": [], "secrecy_refs": [],
        }]
        seed["actors"][0]["intents"] = [
            {**intent, "id": "intent-onstage", "summary": "summary-onstage", "requires_onstage": True,
             "impact_level": "moderate", "scheduled_in": {"amount": 1, "unit": "时"}},
            {**intent, "id": "intent-later", "summary": "summary-later", "requires_onstage": False,
             "impact_level": later_impact, "scheduled_in": {"amount": 3, "unit": "日"}},
        ]
        seed["clocks"] = [{
            "id": "clock-1", "label": "label-1", "owner_ref": "cast-1", "scope": "public",
            "on_expire": "on-expire-1", "due_in": {"amount": 2, "unit": "时"},
        }]
        seed["actors"][0]["intents"].extend(
            {**intent, "id": f"intent-errand-{index:02d}", "summary": f"summary-errand-{index:02d}",
             "requires_onstage": False, "impact_level": "minor", "scheduled_in": {"amount": index + 1, "unit": "时"}}
            for index in range(errands)
        )
        if rival_strikes:
            seed["actors"][1]["agendas"] = [{
                "id": "agenda-r", "goal": "goal-r", "motivation": "", "horizon": "", "priority": 3,
                "means_refs": [], "constraints": [], "secrecy_refs": [],
            }]
            seed["actors"][1]["intents"] = [{
                "id": "intent-strike", "agenda_id": "agenda-r", "action_type": "confront", "target_refs": ["cast-1"],
                "location_ref": "node-2", "summary": "summary-strike", "requires_onstage": False,
                "impact_level": "moderate", "scheduled_in": {"amount": 3, "unit": "日"},
            }]
        return built

    monkeypatch.setattr(world_gate_helpers, "_pack", pack)
    return _seed_world(Session)


def _onstage(world: _World) -> str:
    return world.node("intent", "cast-1/intent-onstage")


def _later(world: _World) -> str:
    return world.node("intent", "cast-1/intent-later")


def _chapter(
    Session, world: _World, number: int, *, days: float = 4,
    protections: Sequence[tuple[str, dict[str, Any]]] = (),
) -> GraphDelta:
    """Commit chapter ``number`` lasting ``days``; protections are review_constraint nodes it creates."""
    chapter = _protecting(_delta(Session, world, number, _time(days, "日")), protections)
    _commit(Session, world, chapter)
    return chapter


def _protecting(chapter: GraphDelta, protections: Sequence[tuple[str, dict[str, Any]]]) -> GraphDelta:
    """P3 has no producer of protections, so the chapter's delta creates them."""
    return chapter.model_copy(update={"narrative_patches": [*chapter.narrative_patches, *(
        NarrativePatch(
            target_ref=f"review_constraint:{node_id}", op="create",
            new_value={"node_type": "review_constraint", "title": node_id, "status": "active", "payload": payload},
        )
        for node_id, payload in protections
    )]})


def _canon_commit(chapter: GraphDelta) -> CanonCommitRecord:
    """The chapter's canon commit as the world step receives it; the tick reads only these fields."""
    return CanonCommitRecord(
        id=f"commit-{chapter.chapter_number}-{chapter.project_id}", project_id=chapter.project_id,
        chapter_number=chapter.chapter_number, graph_delta_ids_json=json.dumps([chapter.id]),
    )


def _tick_id(chapter: GraphDelta) -> str:
    return f"tick-{_canon_commit(chapter).id}"


def _delta_id(chapter: GraphDelta) -> str:
    return f"world-tick:{chapter.project_id}:{_tick_id(chapter)}"


def _service(
    Session, llm: _WorldLLM, *, admission: CanonAdmissionService | None = None, adjudicator: Any = None,
    without_model: bool = False,
) -> WorldTickService:
    return WorldTickService(
        llm_client=None if without_model else llm,
        admission=admission or CanonAdmissionService(),
        adjudicator=adjudicator or WorldAdjudicator(llm),
        actor_runner=ActorTurnRunner(llm),
        conflict_delegate=WorldConflictDelegate(llm),
        audit_writer_factory=own_transaction_audit_writer(Session),
        save_prompt_trace=partial(save_world_prompt_trace, independent=True),
    )


def _tick(Session, llm: _WorldLLM, chapter: GraphDelta, *, policy: RuntimePolicy = POLICY) -> WorldTickResult:
    with Session.begin() as session:
        return _service(Session, llm).run(session, _canon_commit(chapter), policy=policy)


def test_new_actor_budget_drops_second():
    from forwin.world_engine.tick import _Tick
    from tests.test_world_engine_contracts import _new_actor

    tick = object.__new__(_Tick)
    tick.policy = _policy(max_new_actors_per_tick=1)
    tick.conflicts = SimpleNamespace(resolutions=[])
    tick._running_check = lambda: SimpleNamespace(admit=lambda candidate: [])
    dropped = []
    tick._drop_candidate = lambda candidate, reason, issues: dropped.append((candidate, issues))
    candidates = [TickCandidate(candidate_id=str(index), origin="adjudication", summary="Arrival",
                               effects=WorldEffects(new_actors=[_new_actor(name=f"Actor {index}")]))
                  for index in range(2)]
    assert tick._admitted(candidates) == candidates[:1]
    assert dropped[0][0] == candidates[1]
    assert dropped[0][1][0].code == "new_actor_budget"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("node_type", ["character", "group"])
def test_adjudicated_outcome_creates_minor_actor_with_entity_and_personality(monkeypatch, backend, node_type):
    from forwin.canon.entity_admission import EntityAdmissionCommitter
    from forwin.models.entity import Entity
    from tests.test_entity_admission import _tick_entity_session_factory
    from tests.test_world_engine_contracts import _new_actor

    engine, Session = _tick_entity_session_factory(backend)
    try:
        world = _seed_tick_world(Session, monkeypatch)
        chapter = _chapter(Session, world, 1)
        plans = []
        original = EntityAdmissionCommitter.apply

        def capture(self, **kwargs):
            plans.append(kwargs["plan"])
            return original(self, **kwargs)

        monkeypatch.setattr(EntityAdmissionCommitter, "apply", capture)
        llm = _WorldLLM(adjudicate=_outcomes({
            "intent_id": _later(world), "result": "resolved", "summary": "Arrival",
            "effects": {"new_actors": [_new_actor(name="Rowan", node_type=node_type,
                location_id=world.loc(1), affiliation_refs=[world.faction])],
                "events": [{"summary": "Arrival witnessed", "participant_refs": [world.hero, "actor:new:arrival"],
                            "location_id": "", "visibility": "secret", "at": chapter.story_time_end}]},
        }), actor_turn=_quiet_turns)
        result = _tick(Session, llm, chapter)
        assert result.dropped_candidate_ids == []
        runtime = _runtime(Session, world)
        [node] = [node for node in runtime.world.nodes_by_id.values()
                  if node.node_type == node_type and node.metadata.get("tick_id") == result.tick_id]
        assert runtime.world.get_state(node.id)["actor_tier"] == "minor"
        [event_fact] = [f"fact:{fact.id}" for fact in runtime.world.facts_by_id.values()
                        if fact.proposition == "Arrival witnessed"]
        assert _knows(runtime, node.id, event_fact) == "knows"
        assert _knows(runtime, world.hero, event_fact) == "knows"
        with Session() as session:
            entity = session.get(Entity, node.id)
            if node_type == "character":
                assert entity is not None
                assert (entity.name, entity.kind, entity.is_active) == ("Rowan", "character", True)
                assert entity.created_at_chapter == 1
                assert node.profile["personality_loadout"]
                assert runtime.world.get_state(node.id)["alive_status"] == "alive"
                [plan] = plans
                assert plan.candidate_fingerprint == f"world_tick:{result.tick_id}"
                assert plan.decisions[0].entity_id == node.id
            else:
                assert entity is None
        assert _tick(Session, llm, chapter).graph_delta_ids == result.graph_delta_ids
        with Session() as session:
            assert len(session.scalars(select(Entity).where(Entity.id == node.id)).all()) == (node_type == "character")
    finally:
        engine.dispose()


def test_new_actor_name_collision_retries_then_drops(monkeypatch):
    from tests.test_world_engine_contracts import _new_actor

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    name = _runtime(Session, world).world.get_node(world.hero).name
    llm = _WorldLLM(adjudicate=_outcomes({
        "intent_id": _later(world), "result": "resolved", "summary": "Arrival",
        "effects": {"new_actors": [_new_actor(name=name, location_id=world.loc(1))]},
    }), actor_turn=_quiet_turns)
    result = _tick(Session, llm, chapter)
    calls = [content for stage, content in llm.calls if stage == "world.adjudicate"]
    assert len(calls) == 2
    assert "new_actor_name_taken" in calls[1]
    assert result.dropped_candidate_ids == [f"adjudication:{_later(world)}"]
    assert _tick_deltas(Session, world)
    assert not any(node.metadata.get("tick_id") == result.tick_id and node.node_type in {"character", "group"}
                   for node in _runtime(Session, world).world.nodes_by_id.values())
    engine.dispose()


def test_new_actor_budget_commits_first_outcome_postgres(monkeypatch):
    from tests.test_world_engine_contracts import _new_actor

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, rival_strikes=True)
    chapter = _chapter(Session, world, 1)
    intents = sorted([_later(world), _strike(world)])
    llm = _WorldLLM(adjudicate=_outcomes(*[
        {"intent_id": ref, "result": "resolved", "summary": "Arrival",
         "effects": {"new_actors": [_new_actor(name=f"Actor {index}", location_id=world.loc(1),
                                               affiliation_refs=[world.faction])]}}
        for index, ref in enumerate(intents)
    ]), actor_turn=_quiet_turns)
    result = _tick(Session, llm, chapter)
    assert result.dropped_candidate_ids == [f"adjudication:{intents[1]}"]
    runtime = _runtime(Session, world)
    created = [node for node in runtime.world.nodes_by_id.values()
               if node.node_type == "character" and node.metadata.get("tick_id") == result.tick_id]
    assert [node.name for node in created] == ["Actor 0"]
    assert runtime.world.get_state(created[0].id)["actor_tier"] == "minor"
    [dropped] = _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
    assert "new_actor_budget" in [issue["code"] for issue in _payload(dropped)["issues"]]
    assert [runtime.world.get_state(ref)["status"] for ref in intents] == ["resolved", "planned"]
    engine.dispose()


@pytest.mark.parametrize("name", ["New Actor", "new actor", "NewActor", "New\tActor"])
@pytest.mark.parametrize("source", ["node_alias", "candidate"])
def test_new_actor_name_collision_retry_without_database(name, source):
    from forwin.world_engine.constraints import ConstraintIssue
    from forwin.world_engine.delta_builder import _TickBuilder
    from forwin.world_engine.tick import _Tick
    from tests.test_world_adjudicator import CALENDAR, CONTEXT, LEXICON, StubLLM, _runtime as runtime_fixture
    from tests.test_world_engine_contracts import _new_actor

    tick = object.__new__(_Tick)
    tick.runtime, tick.context, tick.lexicon, tick.calendar = runtime_fixture(), CONTEXT, LEXICON, CALENDAR
    if source == "node_alias":
        tick.runtime.world.get_node("owner").aliases = ["New Actor"]
    tick.policy = POLICY
    bad = {"intent_id": "intent-2", "result": "resolved", "summary": "Arrival",
           "effects": {"new_actors": [_new_actor(name=name)]}}
    good = {"intent_id": "intent-1", "result": "resolved", "summary": "Settled", "effects": {}}
    if source == "candidate":
        good["effects"] = {"new_actors": [_new_actor(name="New Actor")]}
    client = StubLLM({"outcomes": [good, bad]}, {"outcomes": [bad]})
    tick.service = SimpleNamespace(llm_client=client, adjudicator=WorldAdjudicator(client))
    tick._check_snapshot = lambda: None
    tick._save_trace = lambda trace: "trace"
    dropped = []
    tick._drop_candidate = lambda candidate, reason, issues: dropped.append((candidate, issues))

    def issues(candidate):
        built = _TickBuilder(context=CONTEXT, tick_id="tick-1", runtime=tick.runtime,
                             calendar=CALENDAR, lexicon=LEXICON).build([candidate], [])
        return [ConstraintIssue(issue.code, issue.message, "") for issue in built.issues if issue.severity == "error"]

    tick._issues = issues
    initial = tick._adjudicate([tick.runtime.world.get_node(ref) for ref in ("intent-1", "intent-2")])
    passed = tick._check(initial.candidates)
    assert [candidate.intent_id for candidate in passed] == ["intent-1"]
    assert len(client.calls) == 2
    assert "new_actor_name_taken" in client.calls[1][0][-1]["content"]
    assert len(dropped) == 1 and dropped[0][0].intent_id == "intent-2"


# reading what the tick left


def _runtime(Session, world: _World, chapter: int = 1) -> BookStateRuntime:
    with Session() as session:
        return BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=chapter)


def _knows(runtime: BookStateRuntime, observer: str, fact_ref: str) -> str | None:
    views = [view for (_kind, observer_id), view in runtime.cognition_by_observer.items() if observer_id == observer]
    return views[0].knowledge_by_ref.get(fact_ref) if views else None


def _tick_deltas(Session, world: _World) -> list[GraphDeltaRow]:
    with Session() as session:
        return list(session.scalars(select(GraphDeltaRow).where(
            GraphDeltaRow.project_id == world.project_id, GraphDeltaRow.source_type == WORLD_ENGINE_SOURCE_TYPE,
        ).order_by(GraphDeltaRow.created_at, GraphDeltaRow.id)))


def _events(Session, world: _World, event_type: str) -> list[DecisionEvent]:
    with Session() as session:
        return list(session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == world.project_id, DecisionEvent.event_type == event_type,
        ).order_by(DecisionEvent.created_at, DecisionEvent.id)))


def _payload(event: DecisionEvent) -> dict[str, Any]:
    return json.loads(event.payload_json)


def _conflicts(Session, world: _World) -> list[WorldPlanConflictRow]:
    with Session() as session:
        return list(session.scalars(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == world.project_id,
        ).order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)))


def _plan(Session, world: _World, number: int) -> None:
    """An active arc whose chapter ``number`` is still to write, in a twenty-chapter book."""
    with Session.begin() as session:
        session.get(Project, world.project_id).target_total_chapters = 20
        arc = ArcPlanVersion(project_id=world.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=10)
        session.add(arc)
        session.flush()
        session.add(ChapterPlan(
            project_id=world.project_id, arc_plan_id=arc.id, chapter_number=number, title=f"title-{number}",
            goals_json=json.dumps([f"goal-{number}"]), experience_plan_json="{}", status="planned",
        ))


def _ambush(world: _World, chapter: GraphDelta) -> dict[str, Any]:
    """The later intent resolves by killing the rival, a named character: a major change."""
    return {
        "intent_id": _later(world), "result": "resolved", "summary": "summary-ambush",
        "effects": {
            "entity_changes": [{"ref": world.rival, "field": "alive_status", "new_value": "dead"}],
            "events": [{"summary": "event-ambush", "participant_refs": [world.hero], "location_id": world.loc(1),
                        "visibility": "public", "at": chapter.story_time_end - 30}],
        },
    }


def _ambushing(world: _World, chapter: GraphDelta, option: str) -> _WorldLLM:
    """The ambush is adjudicated, every actor waits a day, and Codex answers ``option``."""
    return _WorldLLM(adjudicate=_outcomes(_ambush(world, chapter)), actor_turn=_quiet_turns, review=_review(option))


# the brief's tests


@pytest.mark.parametrize("source", ["plan", "pov", "event", "movement"])
def test_tick_preserves_onstage_entities_and_stages_the_rejected_intent(monkeypatch, source):
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    fields = _time(4, "日")
    if source in {"plan", "pov"}:
        _plan(Session, world, 1)
        with Session.begin() as session:
            plan = session.scalar(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id))
            if source == "plan":
                plan.onstage_refs_json = json.dumps([world.rival])
            else:
                plan.pov_ref = world.rival
    elif source == "event":
        fields["events"] = [_event([world.rival])]
    else:
        fields["movements"] = [{"entity_ref": world.rival, "from_ref": world.loc(2),
                                "to_ref": world.loc(1), "evidence": "e"}]
    chapter = _delta(Session, world, 1, fields)
    _commit(Session, world, chapter)
    llm = _ambushing(world, chapter, "accept_with_obligation")
    result = _tick(Session, llm, chapter)
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.rival)["alive_status"] == "alive"
    assert runtime.world.get_state(_later(world))["status"] == "due"
    assert runtime.world.get_node(_later(world)).profile["requires_onstage"] is True
    assert f"adjudication:{_later(world)}" in result.dropped_candidate_ids
    [dropped] = [event for event in _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
                 if _payload(event)["intent_id"] == _later(world)]
    assert _payload(dropped)["reason"] == "touches_onstage_entity"
    asked = [actor["actor_id"] for stage, prompt in llm.calls if stage == "world.actor_turn"
             for actor in json.JSONDecoder().raw_decode(prompt)[0]["actors"]]
    assert world.rival not in asked
    assert not _conflicts(Session, world)
    with Session() as session:
        from forwin.book_state.repository import BookStateRepository
        [delta] = [delta for delta in BookStateRepository(session).list_graph_deltas(world.project_id, through_chapter=1)
                   if delta.source_type == WORLD_ENGINE_SOURCE_TYPE]
        assert any(p.node_id == _later(world) and p.field_path == "profile.requires_onstage"
                   and p.old_value is False and p.new_value is True for p in delta.node_patches)
    engine.dispose()


def test_tick_move_overlapping_the_chapter_move_is_rejected(monkeypatch):
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _delta(Session, world, 1, {**_time(4, "日"), "movements": [{
        "entity_ref": world.rival, "from_ref": world.loc(2), "to_ref": world.loc(1), "evidence": "e",
        "depart_offset": {"amount": 0, "unit": "时"}, "arrive_offset": {"amount": 2, "unit": "时"},
    }]})
    _commit(Session, world, chapter)
    llm = _WorldLLM(adjudicate=_outcomes({
        "intent_id": _later(world), "result": "resolved", "summary": "overlap",
        "effects": {"movements": [{"entity_ref": world.rival, "to_location_id": world.loc(2),
                                    "depart_at": OPENING + 119, "arrive_at": OPENING + 300}]},
    }), actor_turn=_quiet_turns)
    result = _tick(Session, llm, chapter)
    assert f"adjudication:{_later(world)}" in result.dropped_candidate_ids
    [dropped] = [event for event in _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
                 if _payload(event)["intent_id"] == _later(world)]
    assert "movement_overlap" in [issue["code"] for issue in _payload(dropped)["issues"]]
    assert _runtime(Session, world).world.get_state(world.rival)["location_id"] == world.loc(1)
    engine.dispose()


def test_quiet_tick_commits_nothing_and_succeeds() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    chapter = _chapter(Session, world, 1, days=1)
    # An earlier tick put every actor's next turn well past this chapter.
    far = chapter.story_time_end + 10 * DAY
    _commit_tick(Session, world, _built(Session, _context(world, chapter), [TickCandidate(
        candidate_id="setup", origin="actor_turn", summary="setup", effects=WorldEffects(),
        next_turn_at={world.hero: far, world.rival: far, world.faction: far},
    )], tick_id="setup"), tick_id="setup")
    llm = _WorldLLM()

    result = _tick(Session, llm, chapter)

    assert result == WorldTickResult(
        tick_id=_tick_id(chapter), graph_delta_ids=[], conflict_ids=[], dropped_candidate_ids=[],
        postponed_candidate_ids=[],
    )
    assert llm.calls == []
    assert [row.source_id for row in _tick_deltas(Session, world)] == ["setup"]
    assert _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED) == []
    engine.dispose()


def test_tick_resolves_due_intent_propagates_news_and_commits_one_delta(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    now = chapter.story_time_end
    llm = _WorldLLM(
        adjudicate=_outcomes({
            "intent_id": _later(world), "result": "resolved", "summary": "summary-meeting",
            "effects": {
                "events": [{
                    "summary": "event-meeting", "participant_refs": [world.hero], "location_id": world.loc(1),
                    "visibility": "public", "at": now - 30,
                }],
                # The two were never allies, so ending the alliance changes nothing.
                "relation_changes": [
                    {"source_ref": world.hero, "target_ref": world.rival, "edge_type": "ally_of", "change": "end"},
                ],
            },
        }),
        actor_turn=_quiet_turns,
    )

    result = _tick(Session, llm, chapter)

    tick_id = _tick_id(chapter)
    meeting_id = f"adjudication:{_later(world)}"
    [warning] = result.warnings
    assert (warning["code"], warning["candidate_id"]) == ("relation_missing", meeting_id)
    assert result == WorldTickResult(
        tick_id=tick_id, graph_delta_ids=[_delta_id(chapter)], conflict_ids=[], dropped_candidate_ids=[],
        postponed_candidate_ids=[], warnings=[warning],
    )
    assert llm.stages() == ["world.adjudicate", *["world.actor_turn"] * 3]
    [delta] = _tick_deltas(Session, world)
    assert delta.id == _delta_id(chapter)
    runtime = _runtime(Session, world)
    clock = world.node("clock", "clock-1")
    assert runtime.world.get_state(_later(world))["status"] == "resolved"
    assert runtime.world.get_state(_onstage(world))["status"] == "due"
    assert runtime.world.get_state(clock)["status"] == "expired"
    assert {runtime.world.get_state(actor)["next_turn_at"] for actor in (world.hero, world.rival, world.faction)} == {
        now + DAY,
    }
    meeting = tick_event_fact_ref(world.project_id, tick_id, meeting_id, 0)
    expiry = tick_event_fact_ref(world.project_id, tick_id, f"clock:{clock}", 0)
    # The faction is where the meeting happened; the rival is two hours away and hears of it after now.
    assert _knows(runtime, world.faction, meeting) == "knows"
    assert _knows(runtime, world.rival, meeting) is None
    assert json.loads(delta.metadata_json)["news_in_transit"] == [
        {"fact_ref": meeting, "observer_ref": world.rival, "arrive_at": now - 30 + 120},
    ]
    # The clock ran out two hours in, so its news reached the rival long before the chapter ended.
    assert _knows(runtime, world.rival, expiry) == "heard"
    committed = _events(Session, world, DecisionEventType.WORLD_TICK_COMMITTED)
    assert [_payload(event)["tick_id"] for event in committed] == [tick_id]
    engine.dispose()


def test_invalid_actor_intent_retries_then_drops_and_tick_commits_rest() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    chapter = _chapter(Session, world, 1, days=1)
    now = chapter.story_time_end
    # Loc 2 is two hours' walk from the hero, who means to be there in a quarter of an hour.
    hurried = {
        "agenda_ref": "", "action_type": "move", "summary": "summary-hurried", "target_refs": [],
        "location_id": world.loc(2), "scheduled_in": {"amount": 1, "unit": "刻"}, "requires_onstage": False,
        "impact_level": "minor", "expected_effects": {},
    }
    llm = _WorldLLM(actor_turn=_turns(**{world.hero: _turn(world.hero, hurried)}))

    result = _tick(Session, llm, chapter)

    assert llm.stages() == ["world.actor_turn"] * 4
    # The retry asks the hero alone, with the broken rule's code and nothing the hero does not know.
    retry = llm.calls[-1][1]
    assert [actor["actor_id"] for actor in json.JSONDecoder().raw_decode(retry)[0]["actors"]] == [world.hero]
    assert ACTOR_CONSTRAINT_FEEDBACK.format(code="unreachable_before_scheduled") in retry
    assert "cannot reach" not in retry
    hero_turn = f"actor_turn:{world.hero}"
    assert result.dropped_candidate_ids == [hero_turn]
    assert result.graph_delta_ids == [_delta_id(chapter)]
    [dropped] = _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
    payload = _payload(dropped)
    assert (payload["candidate_id"], payload["reason"]) == (hero_turn, "constraint_violation")
    assert [issue["code"] for issue in payload["issues"]] == ["unreachable_before_scheduled"]
    runtime = _runtime(Session, world)
    # The hero keeps its turn for the next tick; the others' turns committed.
    assert runtime.world.get_state(world.hero)["next_turn_at"] == OPENING
    assert runtime.world.get_state(world.rival)["next_turn_at"] == now + DAY
    assert runtime.world.get_state(world.faction)["next_turn_at"] == now + DAY
    assert not any(
        str(node.node_type) == "intent" and node.profile.get("owner_ref") == world.hero
        for node in runtime.world.nodes_by_id.values()
    )
    engine.dispose()


def test_major_impact_goes_to_codex_and_veto_is_applied_in_tick(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    llm = _ambushing(world, chapter, "veto_world")

    result = _tick(Session, llm, chapter)

    [conflict] = _conflicts(Session, world)
    assert result.conflict_ids == [conflict.id]
    assert result.graph_delta_ids == [_delta_id(chapter)]
    assert llm.stages().count("review.world_conflict") == 1
    assert (conflict.source, conflict.violated, conflict.impact_level, conflict.required_decider, conflict.status) == (
        "world_tick", "impact_review", "major", "codex", "resolved",
    )
    held = TickCandidate.model_validate(json.loads(conflict.proposed_change_json))
    assert (held.candidate_id, held.impact_level) == (f"adjudication:{_later(world)}", "major")
    decision = json.loads(conflict.decision_json)
    assert (decision["option"], decision["decided_by"], decision["reason"]) == ("veto_world", "codex", "")
    [resolved] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED)
    payload = _payload(resolved)
    assert payload["trace_id"] and payload["tick_id"] == _tick_id(chapter)
    assert json.loads(conflict.result_refs_json) == [_delta_id(chapter), payload["trace_id"]]
    assert resolved.reason == "rationale-review"
    assert len(_events(Session, world, DecisionEventType.WORLD_CONFLICT_DETECTED)) == 1
    runtime = _runtime(Session, world)
    # The veto lands in the same delta: the rival lives, the intent is vetoed and its owner acts again now.
    assert runtime.world.get_state(world.rival)["alive_status"] != "dead"
    assert runtime.world.get_state(_later(world))["status"] == "vetoed"
    assert runtime.world.get_state(world.hero)["next_turn_at"] == chapter.story_time_end
    held_fact = tick_event_fact_ref(world.project_id, _tick_id(chapter), held.candidate_id, 0)
    assert held_fact.removeprefix("fact:") not in runtime.world.facts_by_id
    engine.dispose()


def test_escalated_conflict_holds_change_and_blocks_next_chapter(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    # Codex may not amend the plan; choosing it hands the conflict to a human.
    llm = _ambushing(world, chapter, "amend_plan")

    result = _tick(Session, llm, chapter)

    [conflict] = _conflicts(Session, world)
    assert result.conflict_ids == [conflict.id]
    assert (conflict.status, conflict.required_decider, conflict.blocks_from_chapter) == ("open", "human", 2)
    with Session() as session:
        repository = WorldConflictRepository(session)
        assert repository.open_blocking(world.project_id, next_chapter=1) is None
        assert repository.open_blocking(world.project_id, next_chapter=2).id == conflict.id
    # The delegate recorded the one escalation; the tick added none.
    assert len(_events(Session, world, DecisionEventType.WORLD_CONFLICT_ESCALATED)) == 1
    assert _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED) == []
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.rival)["alive_status"] != "dead"
    assert runtime.world.get_state(_later(world))["status"] == "planned"
    # The rest of the tick committed; the held event spread no news.
    assert runtime.world.get_state(world.node("clock", "clock-1"))["status"] == "expired"
    [delta] = _tick_deltas(Session, world)
    held_fact = tick_event_fact_ref(world.project_id, _tick_id(chapter), f"adjudication:{_later(world)}", 0)
    assert held_fact not in json.dumps(json.loads(delta.metadata_json)["news_in_transit"])
    assert _knows(runtime, world.faction, held_fact) is None
    expiry = tick_event_fact_ref(world.project_id, _tick_id(chapter), f"clock:{world.node('clock', 'clock-1')}", 0)
    assert _knows(runtime, world.rival, expiry) == "heard"
    engine.dispose()


def test_minor_boundary_violation_is_auto_vetoed(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, later_impact="minor")
    protection = {
        "ref": world.hero, "kind": "must_hold_state", "field": "location_id", "value": world.loc(1),
        "until_chapter": 0, "source": "human",
    }
    chapter = _chapter(Session, world, 1, protections=[("protect-hero-place", protection)])
    now = chapter.story_time_end
    # The hero walks the two-hour road to loc 2, which the protection forbids.
    llm = _WorldLLM(
        adjudicate=_outcomes({
            "intent_id": _later(world), "result": "resolved", "summary": "summary-walk",
            "effects": {"movements": [{
                "entity_ref": world.hero, "to_location_id": world.loc(2), "travel_mode_id": "mode-walk",
                "depart_at": now - 300, "arrive_at": now - 180,
            }]},
        }),
        actor_turn=_quiet_turns,
    )

    result = _tick(Session, llm, chapter)

    [conflict] = _conflicts(Session, world)
    assert result.conflict_ids == [conflict.id]
    assert (conflict.violated, conflict.impact_level, conflict.required_decider, conflict.status) == (
        "protection", "minor", "auto", "resolved",
    )
    assert json.loads(conflict.options_json) == ["veto_world", "amend_plan"]
    decision = json.loads(conflict.decision_json)
    assert (decision["option"], decision["decided_by"]) == ("veto_world", "auto")
    assert "review.world_conflict" not in llm.stages()
    [resolved] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED)
    assert resolved.actor_type == "system"
    [detected] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_DETECTED)
    assert _payload(detected)["violations"] == [
        {"constraint_node_id": "protect-hero-place", "kind": "must_hold_state", "ref": world.hero},
    ]
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.hero)["location_id"] == world.loc(1)
    assert runtime.world.get_state(_later(world))["status"] == "vetoed"
    assert runtime.world.get_state(world.hero)["next_turn_at"] == now
    engine.dispose()


def test_tick_saves_prompt_traces_with_stage_keys(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    llm = _ambushing(world, chapter, "veto_world")

    # The three actors take their turns in one batch, whose call has one trace.
    _tick(Session, llm, chapter, policy=_policy(actor_turn_batch_size=3))

    assert llm.stages() == ["world.adjudicate", "world.actor_turn", "review.world_conflict"]
    with Session() as session:
        traces = list(session.scalars(select(PromptTrace).where(PromptTrace.project_id == world.project_id)))
    stage_keys = sorted(trace.stage_key for trace in traces)
    assert stage_keys == ["review.world_conflict", "world.actor_turn", "world.adjudicate"]
    assert {trace.trace_scope for trace in traces} == {"world_engine"}
    # Each world-role call also left its LLM decision event on its trace.
    succeeded = _events(Session, world, DecisionEventType.LLM_REQUEST_SUCCEEDED)
    world_traces = {trace.id for trace in traces if trace.stage_key.startswith("world.")}
    traced = {event.related_object_id for event in succeeded if event.related_object_type == "prompt_trace"}
    assert traced >= world_traces
    # The resolution names the Codex review that chose it.
    [resolved] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED)
    [review] = [trace for trace in traces if trace.stage_key == "review.world_conflict"]
    assert _payload(resolved)["trace_id"] == review.id
    engine.dispose()


def test_world_step_runs_before_plan_and_records_tick_result() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    chapter = _chapter(Session, world, 1, days=1)
    commit_id = f"commit-step-{world.project_id}"
    with Session.begin() as session:
        arc = ArcPlanVersion(project_id=world.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=10)
        session.add(arc)
        session.flush()
        plan = ChapterPlan(project_id=world.project_id, arc_plan_id=arc.id, chapter_number=1, title="title-1",
                           goals_json="[]", experience_plan_json="{}", status="accepted")
        session.add(plan)
        session.flush()
        draft = ChapterDraft(chapter_plan_id=plan.id, version=1, body_text="body", summary="summary", char_count=4)
        session.add(draft)
        session.flush()
        review = ChapterReview(draft_id=draft.id, verdict="pass")
        session.add(review)
        session.flush()
        candidate = CandidateDraftRecord(
            project_id=world.project_id, chapter_plan_id=plan.id, chapter_number=1, candidate_draft_id=draft.id,
            review_id=review.id, canon_commit_id=commit_id, idempotency_key=f"candidate-{commit_id}",
            status="accepted",
        )
        session.add(candidate)
        session.flush()
        session.add(CanonCommitRecord(
            id=commit_id, idempotency_key=f"canon-{commit_id}", candidate_id=candidate.id,
            project_id=world.project_id, chapter_number=1, graph_delta_ids_json=json.dumps([chapter.id]),
            status="committed",
        ))
    llm = _WorldLLM(actor_turn=_quiet_turns)
    calls: list[str] = []
    tick_service = _service(Session, llm)
    tick_run = tick_service.run

    def tick(session, commit, *, policy):  # noqa: ANN001
        calls.append("tick")
        return tick_run(session, commit, policy=policy)

    tick_service.run = tick  # type: ignore[method-assign]
    service = PostCanonMaintenanceService(
        session_factory=Session,
        comment_analyzer=object(),
        artifact_store=object(),
        llm_client=llm,
        world_tick=tick_service,
    )
    service.heartbeat = lambda _claim: None  # type: ignore[method-assign]
    world_step = service._step_runners["world"]
    service._step_runners = {
        "world": lambda session, commit: calls.append("world") or world_step(session, commit),
        **{
            name: (lambda _session, _commit, name=name: calls.append(name) or {"step": name})
            for name in ("plan", "feedback")
        },
    }

    service.run(canon_commit_id=commit_id, worker_id="worker-1")

    assert POST_CANON_STEP_NAMES == ("world", "plan", "feedback")
    # The world step completes its tick before downstream maintenance.
    assert calls == ["world", "tick", "plan", "feedback"]
    with Session() as session:
        run = session.scalars(select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == commit_id, PostCanonMaintenanceRun.step_name == "world",
        )).one()
    assert run.status == "succeeded"
    tick_id = f"tick-{commit_id}"
    assert json.loads(run.result_json) == {
        "tick_id": tick_id,
        "graph_delta_ids": [f"world-tick:{world.project_id}:{tick_id}"],
        "conflict_ids": [],
        "dropped_candidate_ids": [],
        "postponed_candidate_ids": [],
        "warnings": [],
        "skipped_reason": "",
    }
    engine.dispose()


def test_disabled_world_policy_skips_tick(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    llm = _WorldLLM(adjudicate=_outcomes(), actor_turn=_quiet_turns)

    result = _tick(Session, llm, chapter, policy=_policy(enabled=False))

    assert result == WorldTickResult(
        tick_id=_tick_id(chapter), graph_delta_ids=[], conflict_ids=[], dropped_candidate_ids=[],
        postponed_candidate_ids=[], skipped_reason="world_disabled",
    )
    assert llm.calls == []
    assert _tick_deltas(Session, world) == []
    assert _runtime(Session, world).world.get_state(world.node("clock", "clock-1"))["status"] == "running"
    engine.dispose()


# the dispatch rulings


def test_llm_outage_skips_world_nodes_and_commits_the_deterministic_rest(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    llm = _WorldLLM()

    result = _tick(Session, llm, chapter)

    actors = (world.hero, world.rival, world.faction)
    nodes = [f"adjudication:{_later(world)}", *(f"actor_turn:{actor}" for actor in actors)]
    assert sorted(result.dropped_candidate_ids) == sorted(nodes)
    assert {
        _payload(event)["candidate_id"]: _payload(event)["reason"]
        for event in _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
    } == dict.fromkeys(nodes, "llm_unavailable")
    assert result.graph_delta_ids == [_delta_id(chapter)]
    runtime = _runtime(Session, world)
    # The intent waits for a later tick and the actors keep their turns; the clock and onstage bookkeeping land.
    assert runtime.world.get_state(_later(world))["status"] == "planned"
    assert {runtime.world.get_state(actor)["next_turn_at"] for actor in (world.hero, world.rival, world.faction)} == {
        OPENING,
    }
    assert runtime.world.get_state(world.node("clock", "clock-1"))["status"] == "expired"
    assert runtime.world.get_state(_onstage(world))["status"] == "due"
    # The failed calls still left their traces and failed-request events.
    with Session() as session:
        traces = list(session.scalars(select(PromptTrace).where(PromptTrace.project_id == world.project_id)))
    assert sorted(trace.stage_key for trace in traces) == ["world.actor_turn"] * 3 + ["world.adjudicate"]
    failed = _events(Session, world, DecisionEventType.LLM_REQUEST_FAILED)
    assert {event.related_object_id for event in failed} == {trace.id for trace in traces}
    engine.dispose()


def test_forbidden_delivery_is_requeued_not_dropped() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    # The hero does something in public at loc 1, where the faction also is; the faction may never know of it.
    chapter = _delta(Session, world, 1, {
        **_time(1, "日"), "events": [_event([world.hero], location_ref=world.loc(1))],
    })
    [(_event_item, deed)] = chapter_events(chapter)
    chapter = _protecting(chapter, [("hide-deed", {
        "ref": world.faction, "kind": "must_not_know", "observer_ref": world.faction, "fact_ref": deed,
        "until_chapter": 0, "source": "human",
    })])
    _commit(Session, world, chapter)

    _tick(Session, _WorldLLM(actor_turn=_quiet_turns), chapter)

    # The faction would have witnessed the deed: its news waits for the next tick instead of being lost. The
    # rival, two hours away, still hears of it after the chapter.
    assert _knows(_runtime(Session, world), world.faction, deed) is None
    [delta] = _tick_deltas(Session, world)
    assert json.loads(delta.metadata_json)["news_in_transit"] == [
        {"fact_ref": deed, "observer_ref": world.faction, "arrive_at": chapter.story_time_end + 1},
        {"fact_ref": deed, "observer_ref": world.rival, "arrive_at": chapter.story_time_end + 120},
    ]

    later = _chapter(Session, world, 2, days=1)
    _tick(Session, _WorldLLM(actor_turn=_quiet_turns), later)

    # With the protection still in force, the faction hears the deed once as a rumour, as the rival does, and the
    # news leaves the queue.
    runtime = _runtime(Session, world, 2)
    assert runtime.narrative.nodes_by_id["hide-deed"].payload["until_chapter"] == 0
    assert (_knows(runtime, world.faction, deed), _knows(runtime, world.rival, deed)) == ("heard", "heard")
    [_first, second] = _tick_deltas(Session, world)
    assert [item for item in json.loads(second.metadata_json)["news_in_transit"] if item["fact_ref"] == deed] == []
    engine.dispose()


def test_outcome_carries_its_intents_declared_impact_only_when_resolved() -> None:
    intent = WorldNode(
        id="intent-1", project_id="project-1", node_type="intent", name="intent-1",
        profile={"owner_ref": "actor-1", "impact_level": "major"}, state={"status": "planned"},
    )

    def outcome(result: str) -> TickCandidate:
        return outcome_candidate(intent, AdjudicatedOutcome(
            intent_id="intent-1", result=result, summary=f"summary-{result}", effects=WorldEffects(),
        ))

    resolved, disrupted = outcome("resolved"), outcome("disrupted")
    assert (resolved.candidate_id, resolved.actor_id, resolved.intent_status_changes, resolved.impact_level) == (
        "adjudication:intent-1", "actor-1", {"intent-1": "resolved"}, "major",
    )
    assert (disrupted.intent_status_changes, disrupted.impact_level) == ({"intent-1": "disrupted"}, "minor")


def test_gate_error_in_an_outcome_is_adjudicated_again_with_the_errors(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    now = chapter.story_time_end

    def walk(payload: dict[str, Any], content: str) -> dict[str, Any]:
        # The two-hour road walked in half an hour first; with the gate's errors, in two hours.
        minutes = 120 if "Constraint feedback by intent" in content else 30
        return _outcomes({
            "intent_id": _later(world), "result": "resolved", "summary": "summary-walk",
            "effects": {"movements": [{
                "entity_ref": world.hero, "to_location_id": world.loc(2), "travel_mode_id": "mode-walk",
                "depart_at": now - 300, "arrive_at": now - 300 + minutes,
            }]},
        })(payload, content)

    llm = _WorldLLM(adjudicate=walk, actor_turn=_quiet_turns)

    result = _tick(Session, llm, chapter)

    adjudications = [content for stage, content in llm.calls if stage == "world.adjudicate"]
    assert len(adjudications) == 2
    # Hard rules pass the walk; only the world gate on the outcome's own delta finds it too fast.
    assert "movement_too_fast" in adjudications[1]
    assert result.dropped_candidate_ids == []
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.hero)["location_id"] == world.loc(2)
    assert runtime.world.get_state(_later(world))["status"] == "resolved"
    engine.dispose()


def test_intent_interactions_mirror_chapter_text_without_hard_rules(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    # The chapter fulfils the hero's later intent and the hero dies in it.
    chapter = _delta(Session, world, 1, {
        **_time(1, "日"),
        "entity_changes": [{"ref": world.hero, "field": "alive_status", "new_value": "dead", "evidence": "e"}],
        "intent_interactions": [{"intent_ref": _later(world), "effect": "fulfilled", "evidence": "e"}],
    })
    # A chapter delta that records the interaction but not the status change leaves it to the tick.
    chapter = chapter.model_copy(update={"node_patches": [
        patch for patch in chapter.node_patches
        if not (patch.node_id == _later(world) and patch.field_path == "state.status")
    ]})
    _commit(Session, world, chapter)

    result = _tick(Session, _WorldLLM(actor_turn=_quiet_turns), chapter)

    # A dead owner may not act, but an interaction only mirrors what the chapter already told (Ruling P13).
    assert f"interaction:{_later(world)}" not in result.dropped_candidate_ids
    assert _runtime(Session, world).world.get_state(_later(world))["status"] == "resolved"
    engine.dispose()


def test_tick_skips_a_chapter_that_is_no_longer_the_latest() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    first = _chapter(Session, world, 1, days=1)
    _chapter(Session, world, 2, days=1)
    llm = _WorldLLM(actor_turn=_quiet_turns)

    result = _tick(Session, llm, first)

    assert result == WorldTickResult(
        tick_id=_tick_id(first), graph_delta_ids=[], conflict_ids=[], dropped_candidate_ids=[],
        postponed_candidate_ids=[], skipped_reason="chapter_not_latest",
    )
    assert llm.calls == []
    assert _tick_deltas(Session, world) == []
    engine.dispose()


def test_plot_responds_holds_the_intent_and_commits_no_world_change(monkeypatch) -> None:
    engine, Session = _session_factory()

    def ambushed(*, plan: bool) -> tuple[_World, GraphDelta]:
        world = _seed_tick_world(Session, monkeypatch)
        if plan:
            _plan(Session, world, 2)
        chapter = _chapter(Session, world, 1)
        _tick(Session, _ambushing(world, chapter, "plot_responds"), chapter)
        return world, chapter

    # Only the hold commits; a later band plan will supply its response.
    world, _chapter_one = ambushed(plan=False)
    [conflict] = _conflicts(Session, world)
    assert (conflict.status, conflict.required_decider) == ("resolved", "codex")
    assert json.loads(conflict.decision_json)["pending_plan_response"] is True
    assert _events(Session, world, DecisionEventType.WORLD_CONFLICT_ESCALATED) == []
    assert _runtime(Session, world).world.get_state(world.rival)["alive_status"] == "alive"
    assert _runtime(Session, world).world.get_state(_later(world))["status"] == "held"
    [resolved] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED)
    assert _payload(resolved)["plan_patch_id"] == ""
    with Session() as session:
        assert [row.id for row in WorldConflictRepository(session).pending_plot_responses(world.project_id)] == [conflict.id]

    # With a plan, the hold commits and the next plan is bound to the intent.
    world, chapter = ambushed(plan=True)
    [conflict] = _conflicts(Session, world)
    assert (conflict.status, json.loads(conflict.decision_json)["option"]) == ("resolved", "plot_responds")
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.rival)["alive_status"] == "alive"
    assert runtime.world.get_state(_later(world))["status"] == "held"
    ambush = tick_event_fact_ref(world.project_id, _tick_id(chapter), f"adjudication:{_later(world)}", 0)
    assert ambush not in runtime.cognition_by_observer[("faction", world.faction)].knowledge_by_ref
    with Session() as session:
        plan = session.scalars(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 2,
        )).one()
    # The writer reads the change's public event, never the adjudicator's summary of it.
    assert load_goals_json(plan.goals_json) == [plot_response_goal("event-ambush"), "goal-2"]
    [resolved] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED)
    payload = _payload(resolved)
    assert payload["plan_patch_id"] and payload["graph_delta_ids"] == [_delta_id(chapter)]
    assert json.loads(conflict.result_refs_json) == [_delta_id(chapter), payload["plan_patch_id"], payload["trace_id"]]
    engine.dispose()


def test_major_events_over_the_budget_wait_for_a_later_tick(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, rival_strikes=True)
    chapter = _chapter(Session, world, 1)
    now = chapter.story_time_end
    strike = world.node("intent", "opp-1/intent-strike")
    # Both intents end in a death: two major events where the tick allows one.
    llm = _WorldLLM(
        adjudicate=_outcomes(_ambush(world, chapter), {
            "intent_id": strike, "result": "resolved", "summary": "summary-strike",
            "effects": {"entity_changes": [{"ref": world.hero, "field": "alive_status", "new_value": "dead"}]},
        }),
        actor_turn=_quiet_turns,
    )

    result = _tick(Session, llm, chapter, policy=_policy(max_major_events_per_tick=1, conflict_delegate="human"))

    # Equal tiers and agenda priorities: the budget keeps the first candidate id and postpones the other.
    kept, postponed = sorted((f"adjudication:{_later(world)}", f"adjudication:{strike}"))
    assert result.postponed_candidate_ids == [postponed]
    [conflict] = _conflicts(Session, world)
    assert result.conflict_ids == [conflict.id]
    assert (json.loads(conflict.proposed_change_json)["candidate_id"], conflict.required_decider) == (kept, "human")
    runtime = _runtime(Session, world)
    postponed_intent = postponed.removeprefix("adjudication:")
    assert runtime.world.get_state(postponed_intent)["status"] == "planned"
    assert runtime.world.get_state(postponed_intent)["scheduled_at"] == now
    assert {runtime.world.get_state(actor)["alive_status"] for actor in (world.hero, world.rival)} == {"alive"}
    engine.dispose()


@pytest.mark.parametrize("planless", [False, True], ids=["with-plan", "planless"])
def test_failed_option_side_effect_rolls_back_and_escalates_and_slips_are_not_raised_twice(monkeypatch, planless) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 4)
    with Session.begin() as session:
        if planless:
            session.scalar(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id)).status = "accepted"
        # Three open P1 obligations fill the book's budget.
        for index in range(3):
            session.add(NarrativeObligationRow(
                id=f"existing-{world.project_id}-{index}", project_id=world.project_id, origin_chapter_number=1,
                obligation_type="motivation_gap", priority="P1", status="planned", summary="summary-existing",
                hardness="design_debt", deadline_chapter=5, payoff_test="payoff-existing",
            ))
    with Session.begin() as session:
        arc_id = session.scalar(select(ArcPlanVersion.id).where(ArcPlanVersion.project_id == world.project_id))
        for number in (2, 3):
            session.add(ChapterPlan(
                project_id=world.project_id, arc_plan_id=arc_id, chapter_number=number,
                title=f"bound-{number}", status="accepted",
                world_bindings_json=json.dumps([{"kind": "intent", "ref": _onstage(world)}]),
            ))
    # No slip is allowed: chapters 2 and 3 bind the intent but leave it undelivered.
    policy = _policy(max_onstage_slips=0)
    llm = _WorldLLM(actor_turn=_quiet_turns, review=_review("accept_with_obligation"))
    _tick(Session, llm, _chapter(Session, world, 1, days=1), policy=policy)
    second = _chapter(Session, world, 2, days=1)

    result = _tick(Session, llm, second, policy=policy)

    [conflict] = _conflicts(Session, world)
    assert result.conflict_ids == [conflict.id]
    assert (conflict.violated, conflict.status, conflict.required_decider) == ("onstage_slip", "open", "human")
    [escalated] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_ESCALATED)
    assert _payload(escalated)["failure_reason"] == "option_unavailable"
    assert "obligation_budget_exceeded" in escalated.reason
    assert _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED) == []
    # The accepted slip and its obligation rolled back; the rest of the tick committed without them.
    assert result.graph_delta_ids == [_delta_id(second)]
    runtime = _runtime(Session, world, 2)
    assert runtime.world.get_state(_onstage(world))["slip_count"] == 0
    assert runtime.world.get_state(world.rival)["next_turn_at"] == second.story_time_end + DAY
    with Session() as session:
        assert session.scalars(select(NarrativeObligationRow.id).where(
            NarrativeObligationRow.project_id == world.project_id,
            NarrativeObligationRow.obligation_type == "world_change_response",
        )).all() == []

    # The same slip a chapter later is already open for a human, so the tick raises no second conflict.
    third = _chapter(Session, world, 3, days=1)
    again = _tick(Session, llm, third, policy=policy)
    assert again.conflict_ids == []
    assert [row.id for row in _conflicts(Session, world)] == [conflict.id]
    # The discarded slip leaves a trace naming the conflict that already holds it.
    slip = f"onstage:{_onstage(world)}"
    assert slip in again.dropped_candidate_ids
    [dropped] = [
        _payload(event) for event in _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
        if _payload(event)["tick_id"] == _tick_id(third) and _payload(event)["candidate_id"] == slip
    ]
    assert (dropped["candidate_id"], dropped["reason"], dropped["conflict_id"]) == (slip, "already_held", conflict.id)
    engine.dispose()


# fix round 1


def _rewrite_chapter(Session, world: _World, number: int, chapter_plan_id: str, delta: GraphDelta) -> str:
    """Rewrite accepted chapter ``number`` with ``delta`` as its extraction; the new commit's id."""
    with Session.begin() as session:
        HistoricalCanonRewriteRepository(session).mark_pending(
            project_id=world.project_id, chapter_number=number, reason=f"rewrite chapter {number}",
        )
        plan = session.get(ChapterPlan, chapter_plan_id)
        plan.status = "planned"
        session.flush()
        candidate = _prepare_chapter_candidate(
            session, project_id=world.project_id, chapter=plan, version=2, body=f"Chapter {number} version 2.",
            summary=f"Chapter {number} rewritten.", delta_id="", delta_summary="", node_id="", graph_deltas=[delta],
        )
    outcome = CanonAdmissionService(session_factory=Session).commit_plan(candidate)
    assert not outcome.blocked, outcome.failure_reason
    return outcome.commit_id


def _rewrite_chapter_one(Session, world: _World, chapter_plan_id: str, original: GraphDelta) -> str:
    """Rewrite the latest chapter, 1, with the same extraction under a new delta id; the new commit's id."""
    return _rewrite_chapter(Session, world, 1, chapter_plan_id, original.model_copy(update={"id": f"{original.id}-v2"}))


def _commit_record(Session, commit_id: str) -> CanonCommitRecord:
    with Session() as session:
        record = session.get(CanonCommitRecord, commit_id)
        session.expunge(record)
        return record


@pytest.mark.parametrize("resolved_meanwhile", [False, True], ids=["open", "resolved-meanwhile"])
def test_rewriting_latest_chapter_retires_its_tick_and_reticks(monkeypatch, resolved_meanwhile) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    with Session.begin() as session:
        updater = StateUpdater(session)
        arc = updater.create_arc_plan(world.project_id, "arc")
        plan = updater.create_chapter_plan(world.project_id, arc.id, 1, "c1", "c1 event", ["goal"])
        updater.create_chapter_plan(world.project_id, arc.id, 2, "c2", "c2 event", ["goal"])
        # The chapter binds the onstage intent but never delivers it.
        plan.world_bindings_json = json.dumps([{"kind": "intent", "ref": _onstage(world)}])
        plan_id = plan.id
    original = _delta(Session, world, 1, _time(4, "日"))
    first = _accept(Session, world.project_id, 1, version=1, deltas=[original])
    # No slip is allowed and every conflict waits for a human: each tick holds the slip for a human.
    policy = _policy(max_onstage_slips=0, conflict_delegate="human")
    probed: list[str] = []
    probes: list[str] = []

    def turns(payload: dict[str, Any], content: str) -> dict[str, Any]:
        # While a model is asked, another connection may lock the retired tick's conflict row at once, and a
        # user may decide it.
        for conflict_id in probed:
            with engine.connect() as connection:
                try:
                    probes.append(connection.execute(text(
                        "SELECT status FROM world_plan_conflicts WHERE id = :id FOR UPDATE NOWAIT"
                    ), {"id": conflict_id}).scalar_one())
                except OperationalError:
                    probes.append("locked")
                    continue
                if resolved_meanwhile:
                    connection.execute(text(
                        "UPDATE world_plan_conflicts SET status = 'resolved', decision_option = 'veto_world', decision_json = :decision WHERE id = :id"
                    ), {"id": conflict_id, "decision": json.dumps({"option": "veto_world"})})
                    connection.commit()
                else:
                    connection.rollback()
        return _quiet_turns(payload, content)

    llm = _WorldLLM(adjudicate=_outcomes(), actor_turn=turns)

    def tick(commit_id: str) -> WorldTickResult:
        with Session.begin() as session:
            return _service(Session, llm).run(session, _commit_record(Session, commit_id), policy=policy)

    ticked = tick(first[0])
    _complete_post_canon(Session, world.project_id, 1, first, world_delta_ids=tuple(ticked.graph_delta_ids))
    [held] = _conflicts(Session, world)
    assert (held.violated, held.status, held.origin_key) == ("onstage_slip", "open", f"tick-{first[0]}")
    probed.append(held.id)

    rewritten = _rewrite_chapter_one(Session, world, plan_id, original)

    # The rewrite took the first tick out of canon instead of replaying it, and asked nobody anything.
    assert _tick_deltas(Session, world) == []
    assert json.loads(_commit_record(Session, rewritten).result_json)["historical_rewrite"]["retired_tick_ids"] == [
        f"tick-{first[0]}",
    ]
    assert [row.id for row in _conflicts(Session, world)] == [held.id]

    again = tick(rewritten)

    # No row lock lasted through the re-tick's model calls.
    assert probes[0] == "open" and "locked" not in probes
    # A full tick ran on the rewritten chapter: the retired tick's conflict held nothing for it, so its slip is
    # held again.
    assert again.skipped_reason == ""
    assert again.graph_delta_ids == [f"world-tick:{world.project_id}:tick-{rewritten}"]
    assert [json.loads(row.metadata_json)["tick_id"] for row in _tick_deltas(Session, world)] == [f"tick-{rewritten}"]
    conflicts = {row.id: row for row in _conflicts(Session, world)}
    [fresh] = [row for conflict_id, row in conflicts.items() if conflict_id != held.id]
    assert again.conflict_ids == [fresh.id]
    assert (fresh.violated, fresh.status, fresh.origin_key) == ("onstage_slip", "open", f"tick-{rewritten}")
    retired = conflicts[held.id]
    if resolved_meanwhile:
        # The user's decision stands: the tick expires only what is still open when it commits.
        assert (retired.status, json.loads(retired.decision_json)) == ("resolved", {"option": "veto_world"})
        assert _events(Session, world, DecisionEventType.WORLD_CONFLICT_EXPIRED) == []
    else:
        # The retired tick's conflict expired as the tick committed.
        assert retired.status == "expired"
        assert json.loads(retired.decision_json) == {"reason": "latest chapter rewritten; the world step re-ticks"}
        [event] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_EXPIRED)
        assert (event.related_object_type, event.related_object_id, event.chapter_number) == (
            "world_conflict", held.id, 1,
        )
        assert _payload(event)["reason"] == "latest chapter rewritten; the world step re-ticks"
    assert not any(row.source == "historical_rewrite" for row in conflicts.values())
    with Session() as session:
        assert WorldConflictRepository(session).open_blocking(world.project_id, next_chapter=2).id == fresh.id
    engine.dispose()


def _held(intent_id: str, actor_id: str) -> TickCandidate:
    """An adjudicated outcome that only resolves its intent."""
    return TickCandidate(
        candidate_id=f"adjudication:{intent_id}", origin="adjudication", actor_id=actor_id, intent_id=intent_id,
        summary=f"summary-held-{intent_id}", effects=WorldEffects(), intent_status_changes={intent_id: "resolved"},
    )


def test_older_rewrite_expires_stale_open_conflicts_only(monkeypatch) -> None:
    from forwin.world_engine.conflict_types import ConflictDraft

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, rival_strikes=True)
    with Session.begin() as session:
        updater = StateUpdater(session)
        arc = updater.create_arc_plan(world.project_id, "arc")
        plan_ids = {
            number: updater.create_chapter_plan(world.project_id, arc.id, number, f"c{number}", "event", ["goal"]).id
            for number in (1, 2)
        }
    original = _delta(Session, world, 1, _time(1, "日"))
    first = _accept(Session, world.project_id, 1, version=1, deltas=[original])
    _complete_post_canon(Session, world.project_id, 1, first, world_delta_ids=())
    second = _accept(Session, world.project_id, 2, version=1, deltas=[_delta(Session, world, 2, _time(1, "日"))])
    _complete_post_canon(Session, world.project_id, 2, second, world_delta_ids=())
    strike, later, onstage = _strike(world), _later(world), _onstage(world)

    def conflict(name: str, held: TickCandidate | None, *, after: int = 2, source: str = "world_tick",
                 refs: Sequence[str] = ()) -> str:
        with Session.begin() as session:
            return WorldConflictRepository(session).create(
                ConflictDraft(
                    project_id=world.project_id, source=source, origin_key=f"tick-{name}",
                    detected_after_chapter=after, story_time=0,
                    involved_refs=[*((held.intent_id, held.actor_id) if held else ()), *refs],
                    violated="impact_review" if held else "replay_cas", impact_level="major", summary=f"summary-{name}",
                    proposed_change=held.model_dump(mode="json") if held else {"graph_delta_id": "delta-gone"},
                    intent_ref=held.intent_id if held else "", actor_ref=held.actor_id if held else "",
                    blocks_from_chapter=after + 1,
                ),
                required_decider="human", options=("veto_world", "accept_with_obligation"),
            ).id

    ids = {
        # The rival settles his strike: he is dead in the rewritten chapter 1 (Ruling P30).
        "dead_actor": conflict("dead_actor", _held(strike, world.rival)),
        # The held change resolves the onstage intent the rewritten chapter 1 disrupts (Ruling P22).
        "settled_intent": conflict("settled_intent", _held(onstage, world.hero)),
        "missing_ref": conflict("missing_ref", _held(later, world.hero), refs=["character-never-was"]),
        "fresh": conflict("fresh", _held(later, world.hero)),
        # Detected before the rewritten chapter, or not a world tick's: neither is looked at.
        "earlier": conflict("earlier", _held(strike, world.rival), after=0),
        "replay": conflict("replay", None, source="historical_rewrite"),
    }
    rewritten = _rewrite_chapter(Session, world, 1, plan_ids[1], original.model_copy(update={
        "id": f"{original.id}-v2",
        "node_patches": [
            *original.node_patches,
            NodePatch(node_id=world.rival, node_type="character", op="set", field_path="state.alive_status",
                      old_value="alive", new_value="dead"),
            NodePatch(node_id=onstage, node_type="intent", op="set", field_path="state.status",
                      old_value="planned", new_value="disrupted"),
        ],
    }))
    llm = _WorldLLM(actor_turn=_quiet_turns)

    with Session.begin() as session:
        result = _service(Session, llm).run(session, _commit_record(Session, rewritten), policy=POLICY)

    assert result.skipped_reason == "chapter_not_latest"
    assert llm.calls == []
    assert _tick_deltas(Session, world) == []
    rows = {row.id: row for row in _conflicts(Session, world)}
    statuses = {name: rows[conflict_id].status for name, conflict_id in ids.items()}
    assert statuses == {
        "dead_actor": "expired", "settled_intent": "expired", "missing_ref": "expired",
        "fresh": "open", "earlier": "open", "replay": "open",
    }
    reasons = {name: json.loads(rows[ids[name]].decision_json)["reason"]
               for name in ("dead_actor", "settled_intent", "missing_ref")}
    assert all(reason.startswith("rewrite of chapter 1: ") for reason in reasons.values())
    assert "dead_owner_acts" in reasons["dead_actor"]
    assert onstage in reasons["settled_intent"]
    assert "character-never-was" in reasons["missing_ref"]
    events = _events(Session, world, DecisionEventType.WORLD_CONFLICT_EXPIRED)
    assert sorted(event.related_object_id for event in events) == sorted(
        ids[name] for name in ("dead_actor", "settled_intent", "missing_ref")
    )
    assert {_payload(event)["reason"] for event in events} == set(reasons.values())
    engine.dispose()


def _disrupt_the_later_intent(Session, world: _World, chapter: GraphDelta) -> None:
    """Another canon writer, as a direct conflict resolve does, disrupts the later intent at chapter 1."""
    tick_id = "conflict-race"
    delta = GraphDelta(
        id=f"world-tick:{world.project_id}:{tick_id}", project_id=world.project_id, chapter_number=1,
        story_time_start=chapter.story_time_end, story_time_end=chapter.story_time_end,
        source_type=WORLD_ENGINE_SOURCE_TYPE, source_id=tick_id, summary="summary-race",
        node_patches=[NodePatch(
            node_id=_later(world), node_type="intent", op="set", field_path="state.status",
            old_value="planned", new_value="disrupted",
        )],
        evidence_refs=[f"world_tick:{tick_id}"], metadata={"tick_id": tick_id, "source_chapter": 1},
    )
    with Session.begin() as session:
        CanonAdmissionService().commit_world_tick(
            session=session, project_id=world.project_id, chapter_number=1,
            approved_changes=ApprovedGraphDeltaSet(project_id=world.project_id, chapter_number=1, graph_deltas=[delta]),
            tick_id=tick_id,
        )


def test_canon_written_during_the_tick_makes_it_stale_and_a_retry_commits(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    meeting = {
        "intent_id": _later(world), "result": "resolved", "summary": "summary-meeting",
        "effects": {"events": [{
            "summary": "event-meeting", "participant_refs": [world.hero], "location_id": world.loc(1),
            "visibility": "public", "at": chapter.story_time_end - 30,
        }]},
    }
    raced: list[bool] = []

    def turns(payload: dict[str, Any], content: str) -> dict[str, Any]:
        # While the first actor thinks, another writer changes the canon the tick reasoned on.
        if not raced:
            raced.append(True)
            _disrupt_the_later_intent(Session, world, chapter)
        return _quiet_turns(payload, content)

    first_llm = _WorldLLM(adjudicate=_outcomes(meeting), actor_turn=turns)
    retry_llm = _WorldLLM(adjudicate=_outcomes(meeting), actor_turn=_quiet_turns)
    with pytest.raises(CanonStaleVersion):
        _tick(Session, first_llm, chapter)

    # The step rolled back: only the other writer's delta is left, and the tick never overwrote it.
    assert [row.source_id for row in _tick_deltas(Session, world)] == ["conflict-race"]
    assert _runtime(Session, world).world.get_state(_later(world))["status"] == "disrupted"

    retried = _tick(Session, retry_llm, chapter)

    with Session() as session:
        traces = list(session.scalars(select(PromptTrace).where(PromptTrace.project_id == world.project_id)))
        assert len(traces) == len(first_llm.calls) + len(retry_llm.calls)
        events = list(session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == world.project_id,
            DecisionEvent.event_type == DecisionEventType.LLM_REQUEST_SUCCEEDED,
        )))
        assert {event.related_object_id for event in events} == {trace.id for trace in traces}
    assert retried.graph_delta_ids == [_delta_id(chapter)]
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(_later(world))["status"] == "disrupted"
    assert runtime.world.get_state(world.node("clock", "clock-1"))["status"] == "expired"
    engine.dispose()


@pytest.mark.parametrize(("racing", "skipped"), [
    ("adjudicate", "world.actor_turn"), ("actor_turn", "review.world_conflict"),
], ids=["before_turns", "before_codex"])
def test_stale_snapshot_stops_before_llm_phase(monkeypatch, racing: str, skipped: str) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    handlers: dict[str, Handler] = {
        "adjudicate": _outcomes(_ambush(world, chapter)), "actor_turn": _quiet_turns, "review": _review("veto_world"),
    }
    answer = handlers[racing]

    def racing_answer(payload: dict[str, Any], content: str) -> dict[str, Any] | str:
        # While this phase thinks, another writer changes the canon the tick reasoned on.
        _disrupt_the_later_intent(Session, world, chapter)
        return answer(payload, content)

    llm = _WorldLLM(**{**handlers, racing: racing_answer})

    with pytest.raises(CanonStaleVersion):
        _tick(Session, llm, chapter)

    # The next LLM phase never ran: the stale tick paid for no more calls.
    assert skipped not in llm.stages()
    assert [row.source_id for row in _tick_deltas(Session, world)] == ["conflict-race"]
    assert _conflicts(Session, world) == []
    engine.dispose()


def test_a_conflict_only_tick_on_moved_canon_is_stale(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    # An earlier tick settled the onstage intent and the clock and put every actor's next turn past the chapter,
    # so the ambush, held for review, is all this tick does.
    far = chapter.story_time_end + 10 * DAY
    _commit_tick(Session, world, _built(Session, _context(world, chapter), [TickCandidate(
        candidate_id="setup", origin="actor_turn", summary="setup",
        effects=WorldEffects(entity_changes=[{"ref": world.node("clock", "clock-1"), "field": "status",
                                              "new_value": "expired"}]),
        intent_status_changes={_onstage(world): "disrupted"},
        next_turn_at={world.hero: far, world.rival: far, world.faction: far},
    )], tick_id="setup"), tick_id="setup")
    raced: list[bool] = []

    def review(payload: dict[str, Any], content: str) -> dict[str, Any]:
        # While Codex reviews, another writer changes the canon the tick reasoned on; Codex then hands the
        # conflict to a human, so the tick commits nothing but the conflict.
        raced.append(True)
        _disrupt_the_later_intent(Session, world, chapter)
        return _review("amend_plan")(payload, content)

    llm = _WorldLLM(adjudicate=_outcomes(_ambush(world, chapter)), review=review)

    with pytest.raises(CanonStaleVersion):
        _tick(Session, llm, chapter)

    assert raced == [True]
    assert _conflicts(Session, world) == []
    assert [row.source_id for row in _tick_deltas(Session, world)] == ["setup", "conflict-race"]
    engine.dispose()


def _failing_build(monkeypatch: pytest.MonkeyPatch, candidate_prefix: str | None) -> None:
    """The tick's whole build reports an error on the first candidate whose id starts with ``candidate_prefix``;
    with None, an error of the tick as a whole. The running-state checks' builds are left alone."""
    original = WorldTickDeltaBuilder.build
    monkeypatch.setattr(running_state, "WorldTickDeltaBuilder", type(
        "_UnpatchedBuilder", (WorldTickDeltaBuilder,), {"build": original},
    ))

    def build(self, *, context, candidates, news_in_transit, tick_id, runtime=None):  # noqa: ANN001
        result = original(
            self, context=context, candidates=candidates, news_in_transit=news_in_transit, tick_id=tick_id, runtime=runtime,
        )
        if len(candidates) < 2:
            return result
        if candidate_prefix is None:
            failing = ""
        else:
            matching = [item.candidate_id for item in candidates if item.candidate_id.startswith(candidate_prefix)]
            if not matching:
                return result
            failing = matching[0]
        issue = TickIssue(severity="error", code="injected_error", message="injected", candidate_id=failing)
        return type(result)(None, [*result.issues, issue])

    monkeypatch.setattr(WorldTickDeltaBuilder, "build", build)


def test_final_build_error_drops_the_candidate_and_commits_the_rest(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    # A one-day chapter: the clock runs out and the onstage intent falls due, but nothing is adjudicated.
    chapter = _chapter(Session, world, 1, days=1)
    clock = world.node("clock", "clock-1")
    _failing_build(monkeypatch, "clock:")

    result = _tick(Session, _WorldLLM(adjudicate=_outcomes(), actor_turn=_quiet_turns), chapter)

    assert result.dropped_candidate_ids == [f"clock:{clock}"]
    [dropped] = _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
    assert (_payload(dropped)["reason"], _payload(dropped)["issues"][0]["code"]) == ("build_error", "injected_error")
    assert result.graph_delta_ids == [_delta_id(chapter)]
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(clock)["status"] == "running"
    assert runtime.world.get_state(_onstage(world))["status"] == "due"
    # The dropped clock's event spreads no news.
    expiry = tick_event_fact_ref(world.project_id, _tick_id(chapter), f"clock:{clock}", 0)
    assert _knows(runtime, world.rival, expiry) is None
    [delta] = _tick_deltas(Session, world)
    assert expiry not in json.dumps(json.loads(delta.metadata_json)["news_in_transit"])
    engine.dispose()


def test_final_build_error_on_an_in_tick_option_escalates_its_conflict(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    _failing_build(monkeypatch, "conflict:")

    result = _tick(Session, _ambushing(world, chapter, "veto_world"), chapter)

    [conflict] = _conflicts(Session, world)
    assert result.dropped_candidate_ids == [f"conflict:{conflict.id}:veto"]
    assert (conflict.status, conflict.required_decider) == ("open", "human")
    [escalated] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_ESCALATED)
    assert _payload(escalated)["failure_reason"] == "build_error"
    assert _payload(escalated)["trace_id"]
    assert _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED) == []
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.rival)["alive_status"] != "dead"
    assert runtime.world.get_state(_later(world))["status"] == "planned"
    assert result.graph_delta_ids == [_delta_id(chapter)]
    engine.dispose()


def test_final_build_error_without_a_candidate_raises(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    _failing_build(monkeypatch, None)

    with pytest.raises(RuntimeError, match="does not build"):
        _tick(Session, _WorldLLM(adjudicate=_outcomes(), actor_turn=_quiet_turns), chapter)

    assert _tick_deltas(Session, world) == []
    engine.dispose()


def test_answers_that_never_parse_drop_world_nodes_as_invalid_output(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    # The model answers every call, but never with JSON.
    llm = _WorldLLM(adjudicate=lambda _payload, _content: "not json", actor_turn=lambda _payload, _content: "not json")

    result = _tick(Session, llm, chapter)

    actors = (world.hero, world.rival, world.faction)
    nodes = [f"adjudication:{_later(world)}", *(f"actor_turn:{actor}" for actor in actors)]
    assert sorted(result.dropped_candidate_ids) == sorted(nodes)
    assert {
        _payload(event)["candidate_id"]: _payload(event)["reason"]
        for event in _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
    } == dict.fromkeys(nodes, "invalid_output")
    engine.dispose()


def _settle_every_intent(payload: dict[str, Any], _content: str) -> dict[str, Any]:
    return {"outcomes": [
        {"intent_id": intent["id"], "result": "disrupted", "summary": "summary-blocked", "effects": {}}
        for intent in payload["intents"]
    ]}


def _errands(world: _World, count: int) -> list[str]:
    return [world.node("intent", f"cast-1/intent-errand-{index:02d}") for index in range(count)]


def test_adjudication_takes_the_twenty_earliest_due_intents(monkeypatch) -> None:
    engine, Session = _session_factory()
    # Twenty-four errands fall due hour by hour, then the later intent: twenty-five due intents.
    world = _seed_tick_world(Session, monkeypatch, errands=24)
    chapter = _chapter(Session, world, 1)
    llm = _WorldLLM(adjudicate=_settle_every_intent, actor_turn=_quiet_turns)

    result = _tick(Session, llm, chapter)

    [asked] = [json.JSONDecoder().raw_decode(content)[0] for stage, content in llm.calls if stage == "world.adjudicate"]
    errands = _errands(world, 24)
    assert [intent["id"] for intent in asked["intents"]] == errands[:20]
    runtime = _runtime(Session, world)
    assert {runtime.world.get_state(ref)["status"] for ref in errands[:20]} == {"disrupted"}
    # The rest stay due for the next tick, and none of them is dropped; the result lists them as postponed.
    waiting = [*errands[20:], _later(world)]
    assert {runtime.world.get_state(ref)["status"] for ref in waiting} == {"planned"}
    assert result.dropped_candidate_ids == []
    assert result.postponed_candidate_ids == [f"adjudication:{ref}" for ref in waiting]
    engine.dispose()


class _GuardedAdjudicator:
    """The real adjudicator, except that more than ``limit`` intents, or any of ``oversized``, are over its
    prompt guard."""

    def __init__(self, inner: WorldAdjudicator, limit: int, oversized: Sequence[str] = ()) -> None:
        self.inner = inner
        self.limit = limit
        self.oversized = set(oversized)
        self.batches: list[list[str]] = []

    def fits(self, *, intents, **_kwargs) -> bool:  # noqa: ANN001, ANN003
        return len(intents) <= self.limit and not self.oversized & {intent.id for intent in intents}

    def adjudicate(self, *, context, intents, **kwargs):  # noqa: ANN001, ANN003
        self.batches.append([intent.id for intent in intents])
        if self.fits(intents=intents):
            return self.inner.adjudicate(context=context, intents=intents, **kwargs)
        trace = {"trace_scope": "world_engine", "stage_key": "world.adjudicate",
                 "input_snapshot": context.model_dump(mode="json")}
        ids = tuple(sorted(intent.id for intent in intents))
        return AdjudicationCall(None, trace, ["prompt_too_large"], ids, "prompt_too_large")


def test_oversized_adjudication_packs_the_intents_that_fit_into_one_call(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, errands=24)
    chapter = _chapter(Session, world, 1)
    llm = _WorldLLM(adjudicate=_settle_every_intent, actor_turn=_quiet_turns)
    adjudicator = _GuardedAdjudicator(WorldAdjudicator(llm), limit=10)

    with Session.begin() as session:
        result = _service(Session, llm, adjudicator=adjudicator).run(session, _canon_commit(chapter), policy=POLICY)

    # Twenty are over the guard together, so one call takes the ten earliest that fit; no intent is asked alone.
    errands = _errands(world, 24)
    assert adjudicator.batches == [errands[:10]]
    runtime = _runtime(Session, world)
    assert {runtime.world.get_state(ref)["status"] for ref in errands[:10]} == {"disrupted"}
    waiting = [*errands[10:], _later(world)]
    assert {runtime.world.get_state(ref)["status"] for ref in waiting} == {"planned"}
    assert result.dropped_candidate_ids == []
    assert result.postponed_candidate_ids == [f"adjudication:{ref}" for ref in waiting]
    engine.dispose()


def test_oversized_intent_is_deferred_not_repeated_every_tick(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, errands=24)
    chapter = _chapter(Session, world, 1)
    errands = _errands(world, 24)
    llm = _WorldLLM(adjudicate=_settle_every_intent, actor_turn=_quiet_turns)
    # The earliest errand is over the guard even alone; it must not hold up the others.
    adjudicator = _GuardedAdjudicator(WorldAdjudicator(llm), limit=20, oversized=[errands[0]])

    with Session.begin() as session:
        result = _service(Session, llm, adjudicator=adjudicator).run(session, _canon_commit(chapter), policy=POLICY)

    # One call takes the twenty errands after it; the oversized one is never asked, alone or with the others.
    assert adjudicator.batches == [errands[1:21]]
    runtime = _runtime(Session, world)
    assert {runtime.world.get_state(ref)["status"] for ref in errands[1:21]} == {"disrupted"}
    # It waits for the next tick instead of staying the earliest due intent of every tick.
    assert runtime.world.get_state(errands[0])["status"] == "planned"
    assert runtime.world.get_state(errands[0])["scheduled_at"] == chapter.story_time_end + 1
    assert result.dropped_candidate_ids == [f"adjudication:{errands[0]}"]
    [dropped] = _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
    assert (_payload(dropped)["reason"], _payload(dropped)["issues"][0]["code"]) == (
        "prompt_too_large_deferred", "prompt_too_large",
    )
    waiting = [*errands[21:], _later(world)]
    assert result.postponed_candidate_ids == [f"adjudication:{ref}" for ref in waiting]

    # A tick that ends at the same story minute comes before its new time: it asks only the intents still due.
    same_minute = _chapter(Session, world, 2, days=0)
    with Session.begin() as session:
        _service(Session, llm, adjudicator=adjudicator).run(session, _canon_commit(same_minute), policy=POLICY)

    assert adjudicator.batches[1:] == [waiting]
    assert len(_events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)) == 1

    # Once story time reaches its new time and its prompt fits, a later tick asks and settles it.
    adjudicator.oversized.clear()
    with Session.begin() as session:
        session.get(Project, world.project_id).target_total_chapters = 20
    next_day = _chapter(Session, world, 3, days=1)
    with Session.begin() as session:
        _service(Session, llm, adjudicator=adjudicator).run(session, _canon_commit(next_day), policy=POLICY)

    assert adjudicator.batches[2:] == [[errands[0]]]
    assert _runtime(Session, world, 3).world.get_state(errands[0])["status"] == "disrupted"
    engine.dispose()


def test_retrying_a_committed_tick_returns_its_delta(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    _tick(Session, _WorldLLM(adjudicate=_outcomes(), actor_turn=_quiet_turns), chapter)
    llm = _WorldLLM(adjudicate=_outcomes(), actor_turn=_quiet_turns)

    again = _tick(Session, llm, chapter)

    assert again == WorldTickResult(
        tick_id=_tick_id(chapter), graph_delta_ids=[_delta_id(chapter)], conflict_ids=[], dropped_candidate_ids=[],
        postponed_candidate_ids=[], skipped_reason="tick_already_committed",
    )
    assert llm.calls == []
    assert [row.id for row in _tick_deltas(Session, world)] == [_delta_id(chapter)]
    engine.dispose()


class _ConcurrentlyCommitted(CanonAdmissionService):
    """Another run of the same tick committed its delta while this one was running."""

    def commit_world_tick(self, *, session, project_id, chapter_number, approved_changes, tick_id):  # noqa: ANN001
        return CanonWorldTickOutcome(graph_delta_ids=(f"world-tick:{project_id}:{tick_id}",), idempotent=True)


def test_tick_committed_by_another_run_meanwhile_is_stale(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    llm = _ambushing(world, chapter, "veto_world")

    with pytest.raises(CanonStaleVersion, match="committed by another run"):
        with Session.begin() as session:
            _service(Session, llm, admission=_ConcurrentlyCommitted()).run(
                session, _canon_commit(chapter), policy=POLICY,
            )

    # Nothing of this run stands: no resolution against a delta that lacks its veto.
    assert _conflicts(Session, world) == []
    assert _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED) == []
    engine.dispose()


def test_without_a_model_the_tick_commits_its_deterministic_parts(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    llm = _WorldLLM(adjudicate=_outcomes(), actor_turn=_quiet_turns)

    with Session.begin() as session:
        result = _service(Session, llm, without_model=True).run(session, _canon_commit(chapter), policy=POLICY)

    assert llm.calls == []
    actors = (world.hero, world.rival, world.faction)
    assert {
        _payload(event)["candidate_id"]: _payload(event)["reason"]
        for event in _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
    } == dict.fromkeys(
        [f"adjudication:{_later(world)}", *(f"actor_turn:{actor}" for actor in actors)], "llm_unavailable",
    )
    assert result.graph_delta_ids == [_delta_id(chapter)]
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.node("clock", "clock-1"))["status"] == "expired"
    assert runtime.world.get_state(_onstage(world))["status"] == "due"
    assert runtime.world.get_state(_later(world))["status"] == "planned"
    engine.dispose()


def test_a_maintenance_service_built_without_the_container_keeps_the_tick_off_the_model() -> None:
    llm = _WorldLLM()
    service = PostCanonMaintenanceService(
        session_factory=object(),
        comment_analyzer=object(), artifact_store=object(), llm_client=llm,
    )

    assert service._world_tick_service().llm_client is None


# final fix wave


def _strike(world: _World) -> str:
    return world.node("intent", "opp-1/intent-strike")


def test_two_outcomes_taking_one_item_commit_one_transfer(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, rival_strikes=True)
    chapter = _delta(Session, world, 1, {**_time(4, "日"), "item_transfers": [{
        "new_item": {"name": "item-a", "kind": "item"}, "from_ref": "", "to_ref": world.hero, "evidence": "e",
    }]})
    _commit(Session, world, chapter)
    item = chapter.metadata["item_transfers"][0]["item_id"]
    recipients = {_later(world): world.faction, _strike(world): world.rival}
    # Each outcome takes the same item from the hero; each holds alone, not both together.
    llm = _WorldLLM(
        adjudicate=_outcomes(*(
            {"intent_id": intent_id, "result": "resolved", "summary": f"summary-take-{index}",
             "effects": {"item_transfers": [{"item_ref": item, "from_ref": world.hero, "to_ref": recipient}]}}
            for index, (intent_id, recipient) in enumerate(recipients.items())
        )),
        actor_turn=_quiet_turns,
    )

    result = _tick(Session, llm, chapter)

    # The due intents are adjudicated in id order (same scheduled_at), so the first one's transfer stands.
    first, second = sorted(recipients)
    assert result.dropped_candidate_ids == [f"adjudication:{second}"]
    [dropped] = _events(Session, world, DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED)
    payload = _payload(dropped)
    assert (payload["candidate_id"], payload["reason"]) == (f"adjudication:{second}", "constraint_violation")
    assert "resource_not_held" in [issue["code"] for issue in payload["issues"]]
    [delta] = _tick_deltas(Session, world)
    transfers = json.loads(delta.metadata_json)["item_transfers"]
    assert [(transfer["from_id"], transfer["to_id"]) for transfer in transfers] == [(world.hero, recipients[first])]
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(item)["holder_id"] == recipients[first]
    assert (runtime.world.get_state(first)["status"], runtime.world.get_state(second)["status"]) == (
        "resolved", "planned",
    )
    engine.dispose()


def _duel(world: _World, chapter: GraphDelta) -> _WorldLLM:
    """The hero's later intent kills the rival; the rival's strike moves him to loc 1 and kills the hero."""
    now = chapter.story_time_end
    return _WorldLLM(
        adjudicate=_outcomes(
            {"intent_id": _later(world), "result": "resolved", "summary": "summary-hero-kills",
             "effects": {"entity_changes": [{"ref": world.rival, "field": "alive_status", "new_value": "dead"}]}},
            {"intent_id": _strike(world), "result": "resolved", "summary": "summary-rival-kills",
             "effects": {
                 "entity_changes": [{"ref": world.hero, "field": "alive_status", "new_value": "dead"}],
                 "movements": [{"entity_ref": world.rival, "to_location_id": world.loc(1),
                                "depart_at": now - 300, "arrive_at": now - 150}],
             }},
        ),
        actor_turn=_quiet_turns,
        review=_review("accept_with_obligation"),
    )


def test_an_in_tick_resolution_that_no_longer_holds_goes_to_a_human(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, later_impact="major", rival_strikes=True)
    _plan(Session, world, 2)
    chapter = _chapter(Session, world, 1)

    from forwin.world_engine import tick_conflicts
    original_options = tick_conflicts.available_options
    monkeypatch.setattr(tick_conflicts, "available_options", lambda draft, **kwargs:
                        (*original_options(draft, **kwargs), "accept_with_obligation"))
    # Both deaths are major; accepting them in order makes the second stale.
    result = _tick(Session, _duel(world, chapter), chapter, policy=_policy(max_major_events_per_tick=2))

    first, second = sorted((_later(world), _strike(world)))
    conflicts = {row.intent_ref: row for row in _conflicts(Session, world)}
    assert sorted(conflicts) == [first, second]
    assert (conflicts[first].status, conflicts[second].status) == ("resolved", "open")
    assert conflicts[second].required_decider == "human"
    [escalated] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_ESCALATED)
    assert (_payload(escalated)["conflict_id"], _payload(escalated)["failure_reason"]) == (
        conflicts[second].id, "conflict_stale",
    )
    assert "dead_owner_acts" in escalated.reason
    assert f"adjudication:{second}" in result.dropped_candidate_ids
    runtime = _runtime(Session, world)
    killed = {_later(world): world.rival, _strike(world): world.hero}
    assert {
        actor: runtime.world.get_state(actor)["alive_status"] for actor in (world.hero, world.rival)
    } == {killed[first]: "dead", killed[second]: "alive"}
    assert runtime.world.get_state(second)["status"] == "planned"
    engine.dispose()


def test_cancelled_onstage_resolution_keeps_intent_only_in_open_conflict(monkeypatch):
    from forwin.world_engine.tick import _Tick
    from forwin.world_engine import tick_conflicts

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 2)
    chapter = _chapter(Session, world, 1)
    original_options = tick_conflicts.available_options
    monkeypatch.setattr(tick_conflicts, "available_options", lambda draft, **kwargs:
                        (*original_options(draft, **kwargs), "accept_with_obligation"))
    original_admitted = _Tick._admitted

    def admit_with_onstage_target(tick, base):
        # The resolution was offered before the final running-state screen.
        tick.onstage_refs = frozenset({world.rival})
        return original_admitted(tick, base)

    monkeypatch.setattr(_Tick, "_admitted", admit_with_onstage_target)
    result = _tick(Session, _ambushing(world, chapter, "accept_with_obligation"), chapter)
    [conflict] = _conflicts(Session, world)
    assert (conflict.status, conflict.required_decider) == ("open", "human")
    assert f"adjudication:{_later(world)}" in result.dropped_candidate_ids
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(_later(world))["status"] == "planned"
    assert runtime.world.get_node(_later(world)).profile["requires_onstage"] is False
    assert runtime.world.get_state(world.rival)["alive_status"] == "alive"
    engine.dispose()


def test_resolving_a_held_change_that_canon_has_since_ruled_out_is_stale(monkeypatch) -> None:
    from forwin.world_engine.conflict_resolution import ConflictResolutionService, ConflictStale
    from forwin.world_engine.conflict_types import ConflictDecision

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch, later_impact="major", rival_strikes=True)
    _plan(Session, world, 2)
    chapter = _chapter(Session, world, 1)
    policy = _policy(conflict_delegate="human", max_major_events_per_tick=2)
    _tick(Session, _duel(world, chapter), chapter, policy=policy)
    conflicts = {row.intent_ref: row.id for row in _conflicts(Session, world) if row.status == "open"}

    with Session.begin() as session:
        for conflict_id in conflicts.values():
            row = WorldConflictRepository(session).get(world.project_id, conflict_id)
            row.options_json = json.dumps([*json.loads(row.options_json), "accept_with_obligation"])

    def resolve(intent_id: str) -> None:
        with Session.begin() as session:
            ConflictResolutionService(session, admission=CanonAdmissionService(), policy=policy).resolve(
                project_id=world.project_id, conflict_id=conflicts[intent_id],
                decision=ConflictDecision(option="accept_with_obligation", decided_by="human", actor_id="human",
                                          rationale="r", decided_at=datetime.now(UTC)),
            )

    resolve(_later(world))
    assert _runtime(Session, world).world.get_state(world.rival)["alive_status"] == "dead"

    # The dead rival can no longer move or kill: the second held change is stale, and stays open.
    with pytest.raises(ConflictStale, match="dead_owner_acts"):
        resolve(_strike(world))

    with Session() as session:
        assert WorldConflictRepository(session).get(world.project_id, conflicts[_strike(world)]).status == "open"
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.hero)["alive_status"] == "alive"
    assert runtime.world.get_state(world.rival)["location_id"] == world.loc(2)
    engine.dispose()


def test_a_long_history_keeps_adjudication_under_the_prompt_guard(monkeypatch) -> None:
    from forwin.writer.prompt_core.world_engine import MAX_ADJUDICATION_EVENT_FACTS, MAX_ADJUDICATION_PROMPT_CHARS

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1)
    context = _context(world, chapter)
    # Earlier ticks left 120 public events with the hero, at the hero's own place.
    for batch in range(3):
        _commit_tick(Session, world, _built(Session, context, [TickCandidate.model_validate({
            "candidate_id": f"history-{batch}-{index}", "origin": "adjudication", "summary": "summary-history",
            "effects": {"events": [{"summary": f"event-history-{batch}-{index}", "participant_refs": [world.hero],
                                    "location_id": world.loc(1), "visibility": "public", "at": context.now}]},
        }) for index in range(40)], tick_id=f"history-{batch}"), tick_id=f"history-{batch}")
    llm = _WorldLLM(
        adjudicate=_outcomes({"intent_id": _later(world), "result": "resolved", "summary": "summary-later",
                              "effects": {}}),
        actor_turn=_quiet_turns,
    )

    result = _tick(Session, llm, chapter)

    [content] = [content for stage, content in llm.calls if stage == "world.adjudicate"]
    assert len(content) < MAX_ADJUDICATION_PROMPT_CHARS
    payload = json.JSONDecoder().raw_decode(content)[0]
    assert {node["node_type"] for node in payload["world_nodes"]}.isdisjoint({"event", "intent"})
    assert len([fact for fact in payload["facts"] if fact.get("fact_type") == "event"]) == MAX_ADJUDICATION_EVENT_FACTS
    assert result.dropped_candidate_ids == []
    assert _runtime(Session, world).world.get_state(_later(world))["status"] == "resolved"
    engine.dispose()


def test_a_tick_after_the_books_last_chapter_is_skipped(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    with Session.begin() as session:
        session.get(Project, world.project_id).target_total_chapters = 1
    chapter = _chapter(Session, world, 1)
    llm = _WorldLLM(adjudicate=_outcomes(), actor_turn=_quiet_turns)

    result = _tick(Session, llm, chapter)

    assert result == WorldTickResult(
        tick_id=_tick_id(chapter), graph_delta_ids=[], conflict_ids=[], dropped_candidate_ids=[],
        postponed_candidate_ids=[], skipped_reason="book_complete",
    )
    assert llm.calls == []
    assert _tick_deltas(Session, world) == []
    engine.dispose()


def test_a_quiet_tick_leaves_the_project_row_unlocked() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    chapter = _chapter(Session, world, 1, days=1)
    far = chapter.story_time_end + 10 * DAY
    _commit_tick(Session, world, _built(Session, _context(world, chapter), [TickCandidate(
        candidate_id="setup", origin="actor_turn", summary="setup", effects=WorldEffects(),
        next_turn_at={world.hero: far, world.rival: far, world.faction: far},
    )], tick_id="setup"), tick_id="setup")

    with Session.begin() as session:
        result = _service(Session, _WorldLLM()).run(session, _canon_commit(chapter), policy=POLICY)
        # While the tick's transaction is still open, another canon writer takes the project row at once.
        with Session() as other:
            other.execute(select(Project.id).where(Project.id == world.project_id).with_for_update(
                key_share=True, nowait=True,
            ))
            other.rollback()

    assert result.graph_delta_ids == []
    engine.dispose()


def test_a_quiet_retick_expires_the_retired_ticks_open_conflicts() -> None:
    from forwin.world_engine.conflict_types import ConflictDraft

    engine, Session = _session_factory()
    world = _seed_world(Session)
    chapter = _chapter(Session, world, 1, days=1)
    far = chapter.story_time_end + 10 * DAY
    _commit_tick(Session, world, _built(Session, _context(world, chapter), [TickCandidate(
        candidate_id="setup", origin="actor_turn", summary="setup", effects=WorldEffects(),
        next_turn_at={world.hero: far, world.rival: far, world.faction: far},
    )], tick_id="setup"), tick_id="setup")
    with Session.begin() as session:
        retired = WorldConflictRepository(session).create(
            ConflictDraft(
                project_id=world.project_id, source="world_tick", origin_key="tick-retired", detected_after_chapter=1,
                story_time=0, involved_refs=[], violated="impact_review", impact_level="major",
                summary="summary-retired", proposed_change={}, blocks_from_chapter=2,
            ),
            required_decider="human", options=("veto_world",),
        ).id
    # A rewrite of chapter 1 retired the tick that raised the conflict.
    commit = _canon_commit(chapter)
    commit.result_json = json.dumps({"historical_rewrite": {"retired_tick_ids": ["tick-retired"]}})

    with Session.begin() as session:
        result = _service(Session, _WorldLLM()).run(session, commit, policy=POLICY)

    assert (result.graph_delta_ids, result.skipped_reason) == ([], "")
    [row] = _conflicts(Session, world)
    assert (row.id, row.status) == (retired, "expired")
    [event] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_EXPIRED)
    assert event.related_object_id == retired
    engine.dispose()


def _next_chapter_conflict(Session, world: _World, *, next_status: str) -> str:
    """Chapter 2, paused in ``next_status`` on a conflict its draft raised against chapter 1's text."""
    from forwin.book_state.protections import DeltaBoundaryViolation
    from forwin.world_engine.chapter_conflicts import CHAPTER_CONFLICT_OPTIONS, chapter_conflict_draft

    _plan(Session, world, 2)
    violation = DeltaBoundaryViolation(
        protection_node_id="protect-hero", milestone_ref="", kind="must_be_alive", ref=world.hero, observer_ref="",
        fact_ref="", field="", value=None, until_chapter=0, source="test", impact_level="major", evidence="falls",
    )
    with Session.begin() as session:
        session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 2,
        )).status = next_status
        return WorldConflictRepository(session).create(
            chapter_conflict_draft(violation, project_id=world.project_id, chapter_number=2, story_time=0),
            required_decider="human", options=CHAPTER_CONFLICT_OPTIONS,
        ).id


@pytest.mark.parametrize(("next_status", "rewritten", "world_enabled", "expires"), [
    ("needs_review", True, True, True),
    ("drafted", True, True, True),
    ("needs_review", True, False, True),
    ("planned", True, True, False),
    ("needs_review", False, True, False),
])
def test_rewriting_the_latest_chapter_expires_the_next_chapters_chapter_conflicts(
    next_status, rewritten, world_enabled, expires,
) -> None:
    from forwin.world_engine.conflict_revalidation import rewritten_chapter_reason

    engine, Session = _session_factory()
    world = _seed_world(Session)
    chapter = _chapter(Session, world, 1, days=1)
    far = chapter.story_time_end + 10 * DAY
    _commit_tick(Session, world, _built(Session, _context(world, chapter), [TickCandidate(
        candidate_id="setup", origin="actor_turn", summary="setup", effects=WorldEffects(),
        next_turn_at={world.hero: far, world.rival: far, world.faction: far},
    )], tick_id="setup"), tick_id="setup")
    conflict_id = _next_chapter_conflict(Session, world, next_status=next_status)
    commit = _canon_commit(chapter)
    if rewritten:
        commit.result_json = json.dumps({"historical_rewrite": {"retired_tick_ids": []}})

    with Session.begin() as session:
        _service(Session, _WorldLLM()).run(session, commit, policy=_policy(enabled=world_enabled))

    [row] = _conflicts(Session, world)
    events = _events(Session, world, DecisionEventType.WORLD_CONFLICT_EXPIRED)
    if not expires:
        assert row.status == "open" and events == []
        engine.dispose()
        return
    # Chapter 2's draft was reviewed against the replaced chapter 1: its conflict goes, and its review is retried.
    assert row.status == "expired"
    assert json.loads(row.decision_json) == {"reason": rewritten_chapter_reason(1)}
    assert "chapter 1 rewritten" in rewritten_chapter_reason(1)
    [event] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_EXPIRED)
    assert (event.related_object_id, event.chapter_number, _payload(event)["source"]) == (
        conflict_id, 1, "chapter_extraction",
    )
    with Session() as session:
        assert WorldConflictRepository(session).open_blocking(world.project_id, next_chapter=2) is None
    engine.dispose()


def test_actor_views_read_only_the_turned_actors_recent_vetoes(monkeypatch) -> None:
    from forwin.world_engine.actor_turns import MAX_RECENT_VETOES, recent_vetoes
    from forwin.world_engine.conflict_types import ConflictDecision, ConflictDraft

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    chapter = _chapter(Session, world, 1, days=1)
    with Session.begin() as session:
        repository = WorldConflictRepository(session)
        for index, (actor, option) in enumerate([
            *[(world.hero, "veto_world")] * (MAX_RECENT_VETOES + 2), (world.hero, "plot_responds"),
            (world.rival, "veto_world"),
        ]):
            row = repository.create(ConflictDraft(
                project_id=world.project_id, source="world_tick", detected_after_chapter=1, story_time=index,
                involved_refs=[actor], violated="impact_review", impact_level="major", summary=f"summary-{index}",
                proposed_change={}, actor_ref=actor, blocks_from_chapter=2,
            ), required_decider="human", options=[option])
            repository.mark_resolved(row, decision=ConflictDecision(
                option=option, decided_by="human", rationale="r", decided_at=datetime.now(UTC),
            ), result_refs=[])

    with Session() as session:
        vetoes = recent_vetoes(session, world.project_id, actor_ids=[world.hero, world.faction])
    # Newest first, at most MAX_RECENT_VETOES, vetoes only, and only for the actors asked.
    assert sorted(vetoes) == sorted([world.hero, world.faction])
    assert [row.summary for row in vetoes[world.hero]] == [
        f"summary-{index}" for index in reversed(range(2, MAX_RECENT_VETOES + 2))
    ]
    assert vetoes[world.faction] == []

    # The tick no longer reads every resolved conflict of the book.
    original = WorldConflictRepository.list

    def listing(self, project_id, *, status=None):  # noqa: ANN001
        assert status != "resolved", "the tick listed every resolved conflict"
        return original(self, project_id, status=status)

    monkeypatch.setattr(WorldConflictRepository, "list", listing)
    llm = _WorldLLM(adjudicate=_outcomes(), actor_turn=_quiet_turns)
    _tick(Session, llm, chapter)
    hero_view = next(
        actor for stage, content in llm.calls if stage == "world.actor_turn"
        for actor in json.JSONDecoder().raw_decode(content)[0]["actors"] if actor["actor_id"] == world.hero
    )
    assert len(hero_view["vetoes"]) == MAX_RECENT_VETOES
    engine.dispose()


def test_world_step_result_is_tick_result_only() -> None:
    from unittest.mock import Mock, patch
    service = object.__new__(PostCanonMaintenanceService)
    result = WorldTickResult(tick_id="tick-only", graph_delta_ids=[], conflict_ids=[], dropped_candidate_ids=[], postponed_candidate_ids=[], skipped_reason="world_disabled")
    service.world_tick = SimpleNamespace(run=lambda *args, **kwargs: result)
    commit = SimpleNamespace(project_id="p", chapter_number=1)
    session = Mock()
    with patch("forwin.maintenance.post_canon.ProjectPolicyStore") as store:
        store.return_value.load.return_value.policy = _policy()
        assert service._run_world_step(session, commit) == result.step_result()


def test_change_touching_hard_milestone_is_critical_and_goes_to_human():
    from tests.test_world_tick_steps import _actor, _runtime as memory_runtime
    from forwin.world_engine.constraints import classify_impact
    from forwin.world_engine.conflict_routing import available_options, route_conflict
    from forwin.world_engine.tick_conflicts import review_draft
    from forwin.world_engine.types import EffectEntityChange, TickCandidate, TickContext, WorldEffects

    runtime = memory_runtime(_actor("milestone-actor"))
    candidate = TickCandidate(candidate_id="change", origin="adjudication", summary="change", effects=WorldEffects(
        entity_changes=[EffectEntityChange(ref="milestone-actor", field="state.alive_status", new_value="dead")],
    ))
    refs = frozenset({"milestone-actor"})
    candidate = candidate.model_copy(update={"impact_level": classify_impact(runtime, candidate, milestone_refs=refs)})
    assert candidate.impact_level == "critical"
    review = review_draft(candidate, runtime=runtime, milestone_refs=refs, tick_id="tick-1", context=TickContext(
        project_id=runtime.project_id, chapter_number=3, now=100, window_start=0, seed=1,
    ))
    assert review.draft.violated == "milestone"
    assert route_conflict(review.draft, policy=_policy()) == "human"
    assert available_options(review.draft) == ("veto_world", "amend_plan")
    unrelated = review_draft(candidate, runtime=runtime, milestone_refs=frozenset({"other"}), tick_id="tick-1", context=TickContext(
        project_id=runtime.project_id, chapter_number=3, now=100, window_start=0, seed=1,
    ))
    assert unrelated.draft.violated == "impact_review"


@pytest.mark.parametrize("milestone_actor", ["rival", "hero"])
def test_tick_reads_next_chapters_hard_milestone_refs(monkeypatch, milestone_actor):
    from forwin.models.planning_v6 import ArcBeatSheetRow

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 2)
    protected_actor = getattr(world, milestone_actor)
    with Session.begin() as session:
        arc_id = session.scalar(select(ArcPlanVersion.id).where(ArcPlanVersion.project_id == world.project_id))
        session.add(ArcBeatSheetRow(
            project_id=world.project_id, arc_plan_id=arc_id, protagonist_ref=world.hero,
            milestones_json=json.dumps([{"id": "hard-1", "description": "milestone", "hard": True,
                                         "target_range": [2, 2], "required_refs": [protected_actor]}]),
        ))
    chapter = _chapter(Session, world, 1)
    outcome = _ambush(world, chapter)
    outcome["effects"]["entity_changes"][0]["ref"] = protected_actor
    llm = _WorldLLM(adjudicate=_outcomes(outcome), actor_turn=_quiet_turns, review=_review("veto_world"))
    _tick(Session, llm, chapter, policy=_policy(max_major_events_per_tick=10))
    [conflict] = [row for row in _conflicts(Session, world) if row.intent_ref == _later(world)]
    assert (conflict.impact_level, conflict.violated, conflict.required_decider, conflict.status) == (
        "critical", "milestone", "human", "open",
    )
    assert json.loads(conflict.options_json) == ["veto_world", "amend_plan"]
    assert _runtime(Session, world).world.get_state(protected_actor)["alive_status"] != "dead"
    engine.dispose()


@pytest.mark.parametrize("band_source", ["band_plan", "beat_sheet", "missing"])
def test_tick_slips_unbound_intent_at_band_end_only(monkeypatch, band_source):
    from forwin.models.planning_v6 import ArcBeatSheetRow, BandPlanRow

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 2)
    with Session.begin() as session:
        arc_id = session.scalar(select(ArcPlanVersion.id).where(ArcPlanVersion.project_id == world.project_id))
        if band_source != "missing":
            sheet = ArcBeatSheetRow(
                project_id=world.project_id, arc_plan_id=arc_id, protagonist_ref=world.hero,
                band_breakdown_json=json.dumps([{"band_index": 0, "chapter_start": 1,
                                                 "chapter_end": 8 if band_source == "band_plan" else 2}]),
            )
            session.add(sheet)
            session.flush()
            if band_source == "band_plan":
                session.add(BandPlanRow(project_id=world.project_id, arc_plan_id=arc_id, beat_sheet_id=sheet.id,
                                        band_index=0, chapter_start=1, chapter_end=2))
    llm = _WorldLLM(actor_turn=_quiet_turns)
    _tick(Session, llm, _chapter(Session, world, 1, days=1))
    assert _runtime(Session, world).world.get_state(_onstage(world))["slip_count"] == 0
    _tick(Session, llm, _chapter(Session, world, 2, days=1))
    assert _runtime(Session, world, 2).world.get_state(_onstage(world))["slip_count"] == (
        0 if band_source == "missing" else 1
    )
    engine.dispose()


def test_in_tick_accept_with_obligation_without_next_plan_records_obligation_only(monkeypatch):
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 2)
    with Session.begin() as session:
        plan = session.scalar(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id))
        plan.status = "accepted"
        plan.world_bindings_json = json.dumps([{"kind": "intent", "ref": _onstage(world)}])
    llm = _WorldLLM(actor_turn=_quiet_turns, review=_review("accept_with_obligation"))
    policy = _policy(max_onstage_slips=0)
    _tick(Session, llm, _chapter(Session, world, 1, days=1), policy=policy)
    _tick(Session, llm, _chapter(Session, world, 2, days=1), policy=policy)
    [conflict] = _conflicts(Session, world)
    assert conflict.status == "resolved"
    assert _events(Session, world, DecisionEventType.WORLD_CONFLICT_ESCALATED) == []
    [resolved] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_RESOLVED)
    payload = _payload(resolved)
    assert payload["obligation_id"] and payload["plan_patch_id"] == ""
    with Session() as session:
        obligation = session.get(NarrativeObligationRow, payload["obligation_id"])
        assert obligation.status == "active"
        assert json.loads(obligation.linked_plan_patch_ids_json) == []
    assert _runtime(Session, world, 2).world.get_state(_onstage(world))["slip_count"] == 1
    engine.dispose()


@pytest.mark.parametrize("max_slips", [0, 2], ids=["over-cap", "within-cap"])
def test_milestone_actor_wait_and_onstage_bookkeeping_do_not_open_milestone_conflicts(monkeypatch, max_slips):
    from forwin.models.planning_v6 import ArcBeatSheetRow

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 2)
    with Session.begin() as session:
        plan = session.scalar(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id))
        plan.world_bindings_json = json.dumps([{"kind": "intent", "ref": _onstage(world)}])
        session.add(ArcBeatSheetRow(
            project_id=world.project_id, arc_plan_id=plan.arc_plan_id, protagonist_ref=world.hero,
            milestones_json=json.dumps([{"id": "hard-hero", "description": "milestone", "hard": True,
                                         "target_range": [3, 3], "required_refs": [world.hero]}]),
        ))
    llm = _WorldLLM(actor_turn=_quiet_turns)
    policy = _policy(max_onstage_slips=max_slips, conflict_delegate="human")
    _tick(Session, llm, _chapter(Session, world, 1, days=1), policy=policy)
    assert _conflicts(Session, world) == []
    assert _runtime(Session, world).world.get_state(_onstage(world))["status"] == "due"
    _tick(Session, llm, _chapter(Session, world, 2, days=1), policy=policy)
    conflicts = _conflicts(Session, world)
    assert all(row.violated != "milestone" for row in conflicts)
    if max_slips == 0:
        [conflict] = conflicts
        assert (conflict.impact_level, conflict.violated, conflict.required_decider, conflict.status) == (
            "moderate", "onstage_slip", "human", "open",
        )
    else:
        assert conflicts == []
    assert _runtime(Session, world, 2).world.get_state(_onstage(world))["slip_count"] == (0 if max_slips == 0 else 1)
    engine.dispose()


def test_first_due_in_final_band_tick_is_handed_to_next_planner(monkeypatch):
    from forwin.models.planning_v6 import ArcBeatSheetRow
    from forwin.planning.band_planner.inputs import build_band_inputs
    from forwin.planning.beat_sheet.types import ArcBeatSheetData, BandSpan

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 2)
    with Session.begin() as session:
        plan = session.scalar(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id))
        arc_id = plan.arc_plan_id
        session.add(ArcBeatSheetRow(
            project_id=world.project_id, arc_plan_id=arc_id, protagonist_ref=world.hero,
            band_breakdown_json=json.dumps([{"band_index": 0, "chapter_start": 1, "chapter_end": 1}]),
        ))
    _tick(Session, _WorldLLM(actor_turn=_quiet_turns), _chapter(Session, world, 1, days=1),
          policy=_policy(max_onstage_slips=0))
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(_onstage(world))["status"] == "due"
    assert runtime.world.get_state(_onstage(world))["slip_count"] == 0
    assert _conflicts(Session, world) == []
    with Session() as session:
        band = BandSpan(band_index=1, chapter_start=2, chapter_end=5)
        inputs = build_band_inputs(
            session, project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id),
            beat_sheet=ArcBeatSheetData(protagonist_ref=world.hero, phases=[], band_breakdown=[band]),
            band=band, chapter_numbers=[2, 3, 4, 5], rows={}, as_of=1, runtime=runtime,
            policy=POLICY, notes=[], trope_cost_ceiling=10,
        )
        assert _onstage(world) in inputs.payload["due_onstage_intent_ids"]
    engine.dispose()


def test_declared_critical_bookkeeping_keeps_informational_refs_without_milestone_violation():
    from tests.test_world_tick_steps import _actor, _runtime as memory_runtime
    from forwin.world_engine.constraints import classify_impact
    from forwin.world_engine.tick_conflicts import review_draft
    from forwin.world_engine.types import TickContext

    runtime = memory_runtime(_actor("hero"))
    candidate = TickCandidate(candidate_id="wait", origin="actor_turn", actor_id="hero", summary="wait",
                              effects=WorldEffects(), next_turn_at={"hero": 200}, impact_level="critical")
    refs = frozenset({"hero"})
    assert classify_impact(runtime, candidate, milestone_refs=refs) == "critical"
    review = review_draft(candidate, runtime=runtime, milestone_refs=refs, tick_id="tick-1", context=TickContext(
        project_id=runtime.project_id, chapter_number=1, now=100, window_start=0, seed=1,
    ))
    assert review.draft.violated == "impact_review"
    assert review.draft.involved_refs == ["hero"]


def test_held_intent_is_released_after_its_bound_chapter_without_interaction():
    from tests.test_world_tick_steps import _intent, _runtime as runtime_for
    from forwin.world_engine.intents import release_held_intents

    runtime = runtime_for(_intent("held", "actor", at=5, status="held"),
                          _intent("unbound", "actor", at=5, status="held"))
    [release] = release_held_intents(runtime, bound_intent_refs=frozenset({"held"}), interactions=[], now=100)
    assert release.intent_status_changes == {"held": "planned"}
    assert [(change.ref, change.field, change.new_value) for change in release.effects.entity_changes] == [
        ("held", "scheduled_at", 100),
    ]


@pytest.mark.parametrize("effect,status", [("fulfilled", "resolved"), ("disrupted", "disrupted")])
def test_held_intent_settled_by_its_bound_chapter_stays_settled(effect, status):
    from tests.test_world_tick_steps import _intent, _runtime as runtime_for
    from forwin.world_engine.intents import apply_intent_interactions, release_held_intents

    interactions = [{"intent_id": "held", "effect": effect, "evidence": "chapter"}]
    runtime = runtime_for(_intent("held", "actor", at=5, status="held"))
    assert release_held_intents(runtime, bound_intent_refs=frozenset({"held"}), interactions=interactions, now=100) == []
    assert apply_intent_interactions(runtime, interactions=interactions)[0].intent_status_changes == {"held": status}
    settled = runtime_for(_intent("held", "actor", at=5, status=status))
    assert release_held_intents(settled, bound_intent_refs=frozenset({"held"}), interactions=[], now=100) == []


@pytest.mark.parametrize("effect,expected", [(None, "planned"), ("fulfilled", "resolved"), ("disrupted", "disrupted")])
@pytest.mark.parametrize("last_chapter", [False, True], ids=["continuing", "final"])
def test_tick_releases_or_settles_bound_holds_after_acceptance(monkeypatch, effect, expected, last_chapter):
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 2)
    first = _chapter(Session, world, 1)
    _tick(Session, _ambushing(world, first, "plot_responds"), first)
    assert _runtime(Session, world).world.get_state(_later(world))["status"] == "held"
    with Session.begin() as session:
        plan = session.scalar(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id,
                                                       ChapterPlan.chapter_number == 2))
        plan.status = "accepted"
        if last_chapter:
            session.get(Project, world.project_id).target_total_chapters = 2
    fields = _time(1, "日")
    if effect is not None:
        fields["intent_interactions"] = [{"intent_ref": _later(world), "effect": effect, "evidence": "chapter"}]
    second = _delta(Session, world, 2, fields)
    _commit(Session, world, second)
    _tick(Session, _WorldLLM(actor_turn=_quiet_turns), second)
    state = _runtime(Session, world, chapter=2).world.get_state(_later(world))
    assert state["status"] == expected
    if effect is None:
        assert state["scheduled_at"] == second.story_time_end
    assert _runtime(Session, world, chapter=2).world.get_state(world.rival)["alive_status"] == "alive"
    engine.dispose()


def test_tick_reuses_runtime_for_builds_and_gate(monkeypatch):
    from forwin.book_state.reviewer import BookStateReviewGate

    original_create_project = StateUpdater.create_project
    def create_project(self, *args, **kwargs):
        # Separate databases share one project id so seeded and tick-generated refs compare exactly.
        with monkeypatch.context() as ids:
            ids.setattr("forwin.state.updater.new_id", lambda: "runtime-reuse-project")
            return original_create_project(self, *args, **kwargs)
    monkeypatch.setattr(StateUpdater, "create_project", create_project)
    original_load = BookStateProjection.load_runtime_as_of
    original_build = WorldTickDeltaBuilder.build
    original_review = BookStateReviewGate.review
    loads = []
    def load(self, *args, **kwargs):
        loads.append(kwargs["as_of_chapter"])
        return original_load(self, *args, **kwargs)
    monkeypatch.setattr(BookStateProjection, "load_runtime_as_of", load)
    counts = []
    patches = []
    for reuse in (True, False):
        engine, Session = _session_factory()
        world = _seed_tick_world(Session, monkeypatch, errands=24)
        chapter = _chapter(Session, world, 1)
        def build(self, **kwargs):
            if not reuse:
                kwargs["runtime"] = None
            return original_build(self, **kwargs)
        def review(self, changes, **kwargs):
            if not reuse:
                kwargs["runtime"] = None
            return original_review(self, changes, **kwargs)
        monkeypatch.setattr(WorldTickDeltaBuilder, "build", build)
        monkeypatch.setattr(BookStateReviewGate, "review", review)
        loads.clear()
        result = _tick(Session, _WorldLLM(adjudicate=_settle_every_intent, actor_turn=_quiet_turns), chapter)
        counts.append(len(loads))
        assert result.dropped_candidate_ids == []
        runtime = _runtime(Session, world)
        assert {runtime.world.get_state(ref)["status"] for ref in _errands(world, 24)[:20]} == {"disrupted"}
        with Session() as session:
            [delta] = [
                delta for delta in BookStateProjection(session).repo.list_graph_deltas(
                    world.project_id, through_chapter=1,
                ) if delta.id in result.graph_delta_ids
            ]
        patches.append((delta.node_patches, delta.fact_patches, delta.narrative_patches))
        engine.dispose()
    assert counts[0] < counts[1] / 2, counts
    assert patches[0] == patches[1]


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_snapshot_order_is_monotonic_within_one_transaction(backend):
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SASession
    from forwin.models import Base
    from forwin.models.book_state import WorldSnapshotRow
    from tests.postgres import postgres_test_url
    from forwin.book_state.repository import BookStateRepository

    engine = create_engine("sqlite:///:memory:" if backend == "sqlite" else postgres_test_url("snapshot-seq"))
    Base.metadata.create_all(engine)
    with SASession(engine) as session, session.begin():
        session.add(Project(id="snapshot-project", title="title", premise="premise", genre="test"))
        session.flush()
        stamp = datetime(2026, 10, 22)
        first = WorldSnapshotRow(id="z-first", project_id="snapshot-project", as_of_chapter=2, built_at=stamp)
        session.add(first)
        session.flush()
        second = WorldSnapshotRow(id="a-second", project_id="snapshot-project", as_of_chapter=2,
                                  built_at=stamp - timedelta(seconds=1))
        session.add(second)
        session.flush()
        repo = BookStateRepository(session)
        assert repo.latest_world_snapshot_id("snapshot-project", 2) == "a-second"
        assert repo.latest_world_snapshot("snapshot-project", 2).id == "a-second"
        assert second.seq > first.seq > 0
        session.add(WorldSnapshotRow(id="later-seq-earlier-chapter", project_id="snapshot-project", as_of_chapter=1))
        session.flush()
        assert repo.latest_world_snapshot_id("snapshot-project", 2) == "a-second"
    engine.dispose()


def test_drift_candidate_commits_in_tick(monkeypatch):
    from tests.test_world_drift import variable
    from forwin.world_engine.drift import WorldDriftRunner

    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    node = variable().model_copy(update={"project_id": world.project_id})
    node.profile.update(scope_refs=[world.loc(1)], holder_refs=[world.hero])
    node.state["last_drift_at"] = OPENING
    chapter = _delta(Session, world, 1, _time(4, "日"))
    chapter = chapter.model_copy(update={"node_patches": [*chapter.node_patches, NodePatch(
        node_id=node.id, node_type="world_variable", op="create",
        new_value=node.model_dump(mode="json", exclude={"id", "node_type"}),
    )]})
    _commit(Session, world, chapter)
    llm = _WorldLLM(drift=lambda payload, content: {"changes": [{
        "variable_id": "condition", "new_level": 2, "trend": "rising", "summary": "drift-summary",
    }]})
    with Session.begin() as session:
        service = _service(Session, llm)
        service.drift_runner = WorldDriftRunner(llm)
        result = service.run(session, _canon_commit(chapter), policy=_policy(
            drift_every_story_minutes=DAY, max_actor_turns_per_tick=0))
    assert "drift:condition" not in result.dropped_candidate_ids
    assert _runtime(Session, world).world.get_state("condition") == {
        "level": 2, "trend": "rising", "last_drift_at": OPENING + 4 * DAY, "status": "active",
    }
    with Session() as session:
        from forwin.book_state.repository import BookStateRepository
        deltas = BookStateRepository(session).list_graph_deltas(world.project_id, through_chapter=1)
        patches = [p for d in deltas if d.source_type == WORLD_ENGINE_SOURCE_TYPE
                   for p in d.node_patches if p.node_id == "condition"]
        assert {p.field_path: p.old_value for p in patches} == {
            "state.level": 1, "state.trend": "stable", "state.last_drift_at": OPENING,
        }
        traces = list(session.scalars(select(PromptTrace).where(PromptTrace.project_id == world.project_id)))
        assert any(trace.stage_key == "world.drift" for trace in traces)
    assert any(_payload(event).get("stage_key") == "world.drift"
               for event in _events(Session, world, DecisionEventType.LLM_REQUEST_SUCCEEDED))
    engine.dispose()
