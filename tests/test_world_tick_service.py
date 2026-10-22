from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.runtime import BookStateRuntime
from forwin.canon import CanonAdmissionService
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
from forwin.protocol.book_state import GraphDelta, NarrativePatch, WorldNode
from forwin.runtime.policy import RuntimePolicy
from forwin.simulation.world import WorldTurnDraft
from forwin.world_engine.actor_turns import ActorTurnRunner
from forwin.world_engine.adjudicator import WorldAdjudicator
from forwin.world_engine.conflict_delegate import WorldConflictDelegate
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.delta_builder import tick_event_fact_ref
from forwin.world_engine.propagation import chapter_events
from forwin.world_engine.tick import WorldTickResult, WorldTickService
from forwin.world_engine.tick_nodes import outcome_candidate
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE, AdjudicatedOutcome, TickCandidate, WorldEffects
from forwin.writer.prompt_core.world_engine import ACTOR_CONSTRAINT_FEEDBACK, plot_response_goal
from tests import test_world_gate_v6
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
_STAGES = {"world.adjudicate": "adjudicate", "world.actor_turn": "actor_turn", "review.world_conflict": "review"}


def _policy(**world: Any) -> RuntimePolicy:
    return POLICY.model_copy(update={"world": POLICY.world.model_copy(update=world)})


# a stub model client for the three world roles


Handler = Callable[[dict[str, Any], str], dict[str, Any]]


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
        answer = json.dumps(handler(json.JSONDecoder().raw_decode(content)[0], content), ensure_ascii=False)
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
) -> _World:
    """An onstage intent due an hour in, an offstage one three days in (``later_impact``), a clock two hours in.

    With ``rival_strikes`` the rival also means to strike at the hero three days in, offstage.
    """
    original = test_world_gate_v6._pack

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

    monkeypatch.setattr(test_world_gate_v6, "_pack", pack)
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


def _service(Session, llm: _WorldLLM) -> WorldTickService:
    return WorldTickService(
        llm_client=llm,
        admission=CanonAdmissionService(),
        adjudicator=WorldAdjudicator(llm),
        actor_runner=ActorTurnRunner(llm),
        conflict_delegate=WorldConflictDelegate(llm),
        audit_writer_factory=own_transaction_audit_writer(Session),
        save_prompt_trace=save_world_prompt_trace,
    )


def _tick(Session, llm: _WorldLLM, chapter: GraphDelta, *, policy: RuntimePolicy = POLICY) -> WorldTickResult:
    with Session.begin() as session:
        return _service(Session, llm).run(session, _canon_commit(chapter), policy=policy)


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


def test_world_step_runs_before_planning_and_records_tick_result() -> None:
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
    service = PostCanonMaintenanceService(
        session_factory=Session,
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=SimpleNamespace(
            simulate=lambda **_kwargs: WorldTurnDraft(pressure_level="low", pressure_summary="", notable_shifts=[]),
        ),
        artifact_store=object(),
        llm_client=llm,
        world_tick=_service(Session, llm),
    )
    service.heartbeat = lambda _claim: None  # type: ignore[method-assign]
    calls: list[str] = []
    world_step = service._step_runners["world"]
    service._step_runners = {
        "world": lambda session, commit: calls.append("world") or world_step(session, commit),
        **{
            name: (lambda _session, _commit, name=name: calls.append(name) or {"step": name})
            for name in ("planning", "arc", "feedback")
        },
    }

    service.run(canon_commit_id=commit_id, worker_id="worker-1")

    assert POST_CANON_STEP_NAMES == ("world", "planning", "arc", "feedback")
    assert calls == list(POST_CANON_STEP_NAMES)
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
        "pressure_level": "low",
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


def test_news_leaves_out_what_a_protection_forbids_an_observer_to_know() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    # The hero does something in public at loc 1, where the faction also is.
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

    # The faction would have witnessed the deed; the rival, two hours away, still hears of it after the chapter.
    assert _knows(_runtime(Session, world), world.faction, deed) is None
    [delta] = _tick_deltas(Session, world)
    assert json.loads(delta.metadata_json)["news_in_transit"] == [
        {"fact_ref": deed, "observer_ref": world.rival, "arrive_at": chapter.story_time_end + 120},
    ]
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


def test_codex_plot_response_runs_in_tick_or_escalates_without_an_unwritten_plan(monkeypatch) -> None:
    engine, Session = _session_factory()

    def ambushed(*, plan: bool) -> tuple[_World, GraphDelta]:
        world = _seed_tick_world(Session, monkeypatch)
        if plan:
            _plan(Session, world, 2)
        chapter = _chapter(Session, world, 1)
        _tick(Session, _ambushing(world, chapter, "plot_responds"), chapter)
        return world, chapter

    # Without an unwritten plan to respond in, the choice cannot run: a human decides and the change is held.
    world, _chapter_one = ambushed(plan=False)
    [conflict] = _conflicts(Session, world)
    assert (conflict.status, conflict.required_decider) == ("open", "human")
    [escalated] = _events(Session, world, DecisionEventType.WORLD_CONFLICT_ESCALATED)
    assert (_payload(escalated)["failure_reason"], _payload(escalated)["option"]) == (
        "no_unaccepted_plan", "plot_responds",
    )
    assert _runtime(Session, world).world.get_state(world.rival)["alive_status"] != "dead"

    # With one, the held change commits in the tick's own delta, its news spreads and the next plan responds.
    world, chapter = ambushed(plan=True)
    [conflict] = _conflicts(Session, world)
    assert (conflict.status, json.loads(conflict.decision_json)["option"]) == ("resolved", "plot_responds")
    runtime = _runtime(Session, world)
    assert runtime.world.get_state(world.rival)["alive_status"] == "dead"
    assert runtime.world.get_state(_later(world))["status"] == "resolved"
    ambush = tick_event_fact_ref(world.project_id, _tick_id(chapter), f"adjudication:{_later(world)}", 0)
    assert _knows(runtime, world.faction, ambush) == "knows"
    with Session() as session:
        plan = session.scalars(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 2,
        )).one()
    assert load_goals_json(plan.goals_json) == [plot_response_goal("summary-ambush"), "goal-2"]
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


def test_failed_option_side_effect_rolls_back_and_escalates_and_slips_are_not_raised_twice(monkeypatch) -> None:
    engine, Session = _session_factory()
    world = _seed_tick_world(Session, monkeypatch)
    _plan(Session, world, 4)
    with Session.begin() as session:
        # Three open P1 obligations fill the book's budget.
        for index in range(3):
            session.add(NarrativeObligationRow(
                id=f"existing-{world.project_id}-{index}", project_id=world.project_id, origin_chapter_number=1,
                obligation_type="motivation_gap", priority="P1", status="planned", summary="summary-existing",
                hardness="design_debt", deadline_chapter=5, payoff_test="payoff-existing",
            ))
    # No slip is allowed: the onstage intent, due after chapter 1, conflicts once chapter 2 leaves it alone.
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
    assert _tick(Session, llm, third, policy=policy).conflict_ids == []
    assert [row.id for row in _conflicts(Session, world)] == [conflict.id]
    engine.dispose()
