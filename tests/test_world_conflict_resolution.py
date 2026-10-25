from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from forwin.audit.events import DecisionEventType
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state import BookStateCompiler
from forwin.canon import CanonAdmissionService, CanonStaleVersion, CanonWriteFailure
from forwin.models import ArcPlanVersion, ChapterPlan, NarrativeObligationRow, NarrativePlanPatchRow, Project
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import GraphDeltaRow
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.goals import load_goals_json
from forwin.protocol.book_state import GraphDelta, NarrativePatch, WorldNode
from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.conflict_repository import ConflictNotOpen, WorldConflictRepository
from forwin.world_engine.conflict_resolution import (
    ConflictOptionUnavailable,
    ConflictResolutionResult,
    ConflictResolutionService,
    ConflictStale,
    option_candidates,
)
from forwin.world_engine.conflict_types import ConflictDecision, ConflictDraft
from forwin.world_engine.constraints import check_narrative_boundaries
from forwin.world_engine.delta_builder import tick_event_fact_ref
from forwin.world_engine.propagation import latest_news_in_transit
from forwin.world_engine.types import (
    WORLD_ENGINE_SOURCE_TYPE,
    EffectEntityChange,
    EffectEvent,
    EffectMovement,
    EffectRelationChange,
    NewsItem,
    TickCandidate,
    WorldEffects,
)
from forwin.writer.prompt_core.world_engine import (
    ONSTAGE_SLIP_RESPONSE_GOAL,
    PLOT_RESPONSE_GENERIC_GOAL,
    plot_response_goal,
)
from tests.test_world_gate_v6 import _commit, _delta, _session_factory, _time, _World
from tests.test_world_tick_delta import _build, _built, _commit_tick, _context, _issues, _patches
from tests.test_world_tick_steps import _seed_steps_world


TOTAL_CHAPTERS = 20
SUMMARY = "summary-conflict"
ALL_OPTIONS = ["veto_world", "plot_responds", "amend_plan", "accept_with_obligation"]
POLICY = RuntimePolicy.for_profile("standard")


@dataclass(frozen=True)
class _Book:
    world: _World
    chapter: GraphDelta
    # ChapterPlan id by chapter number: chapter 1 is accepted, 2 and 3 are still to write.
    plans: dict[int, str]

    @property
    def project_id(self) -> str:
        return self.world.project_id

    @property
    def now(self) -> int:
        return self.chapter.story_time_end

    @property
    def onstage(self) -> str:
        return self.world.node("intent", "cast-1/intent-onstage")

    @property
    def later(self) -> str:
        return self.world.node("intent", "cast-1/intent-later")


def _book(
    Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    protections: Callable[[_World], Sequence[tuple[str, dict[str, Any]]]] = lambda world: (),
) -> _Book:
    """The steps world with an arc of chapter plans and a committed one-day chapter 1."""
    world = _seed_steps_world(Session, monkeypatch)
    plans: dict[int, str] = {}
    with Session.begin() as session:
        session.get(Project, world.project_id).target_total_chapters = TOTAL_CHAPTERS
        arc = ArcPlanVersion(project_id=world.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=10)
        session.add(arc)
        session.flush()
        for number in (1, 2, 3):
            plan = ChapterPlan(
                project_id=world.project_id, arc_plan_id=arc.id, chapter_number=number, title=f"title-{number}",
                goals_json=json.dumps([f"goal-{number}"]), experience_plan_json="{}",
                status="accepted" if number == 1 else "planned",
            )
            session.add(plan)
            session.flush()
            plans[number] = plan.id
    chapter = _delta(Session, world, 1, _time(1, "日"))
    # Protections are narrative review_constraint nodes; P3 has no producer, so the chapter carries them.
    chapter = chapter.model_copy(update={"narrative_patches": [
        *chapter.narrative_patches,
        *(
            NarrativePatch(
                target_ref=f"review_constraint:{node_id}", op="create",
                new_value={"node_type": "review_constraint", "title": node_id, "status": "active", "payload": payload},
            )
            for node_id, payload in protections(world)
        ),
    ]})
    _commit(Session, world, chapter)
    return _Book(world, chapter, plans)


def _held(book: _Book, intent_id: str = "") -> TickCandidate:
    """An adjudicated outcome held for review: an intent (the later one by default) resolves with a
    public event at loc 1, where the hero and the faction are; the rival is two hours away at loc 2."""
    intent_id = intent_id or book.later
    return TickCandidate(
        candidate_id="adjudication:held",
        origin="adjudication",
        actor_id=book.world.hero,
        intent_id=intent_id,
        summary="summary-held",
        effects=WorldEffects(events=[EffectEvent(
            summary="event-held", participant_refs=[book.world.hero], location_id=book.world.loc(1),
            visibility="public", at=book.now,
        )]),
        intent_status_changes={intent_id: "resolved"},
        impact_level="major",
    )


def _slip(book: _Book, intent_id: str) -> dict[str, Any]:
    """An onstage slip past the cap, as intents.onstage_bookkeeping holds it."""
    return TickCandidate(
        candidate_id=f"onstage:{intent_id}", origin="onstage", actor_id=book.world.hero, intent_id=intent_id,
        summary="summary-slip", effects=WorldEffects(), slip_updates={intent_id: 3},
    ).model_dump(mode="json")


def _conflict(
    Session,
    book: _Book,
    *,
    proposed_change: dict[str, Any],
    options: Sequence[str] = tuple(ALL_OPTIONS),
    source: str = "world_tick",
    violated: str = "impact_review",
    impact: str = "moderate",
    intent_ref: str = "",
    actor_ref: str = "",
    detected_after_chapter: int = 1,
) -> str:
    with Session.begin() as session:
        row = WorldConflictRepository(session).create(
            ConflictDraft(
                project_id=book.project_id, source=source, detected_after_chapter=detected_after_chapter,
                story_time=book.now,
                involved_refs=[ref for ref in (intent_ref, actor_ref) if ref], violated=violated,
                impact_level=impact, summary=SUMMARY, proposed_change=proposed_change,
                intent_ref=intent_ref or str(proposed_change.get("intent_id") or ""),
                actor_ref=actor_ref, blocks_from_chapter=2,
            ),
            required_decider="human",
            options=list(options),
        )
        return row.id


def _decision(option: str, decided_by: str = "human", reason: str = "") -> ConflictDecision:
    return ConflictDecision(
        option=option, decided_by=decided_by, actor_id="reviewer-1", rationale="rationale-1", reason=reason,
        decided_at=datetime(2026, 10, 22, tzinfo=UTC),
    )


def _policy(**world: Any) -> RuntimePolicy:
    return POLICY.model_copy(update={"world": POLICY.world.model_copy(update=world)})


def _service(session, *, policy: RuntimePolicy = POLICY, admission: CanonAdmissionService | None = None):
    return ConflictResolutionService(session, admission=admission or CanonAdmissionService(), policy=policy)


def _resolve(
    Session,
    book: _Book,
    conflict_id: str,
    option: str,
    *,
    decided_by: str = "human",
    reason: str = "",
    policy: RuntimePolicy = POLICY,
) -> ConflictResolutionResult:
    with Session.begin() as session:
        return _service(session, policy=policy).resolve(
            project_id=book.project_id, conflict_id=conflict_id, decision=_decision(option, decided_by, reason),
        )


def _resolve_failing(
    Session,
    book: _Book,
    conflict_id: str,
    option: str,
    error: type[Exception],
    *,
    decided_by: str = "human",
    admission: CanonAdmissionService | None = None,
    match: str | None = None,
) -> None:
    """Resolving raises ``error`` inside a transaction that still commits afterwards."""
    with Session.begin() as session:
        service = _service(session, admission=admission)
        with pytest.raises(error, match=match):
            service.resolve(project_id=book.project_id, conflict_id=conflict_id, decision=_decision(option, decided_by))
        # Only the resolution rolled back: the session still reads and the conflict is still open.
        assert WorldConflictRepository(session).get(book.project_id, conflict_id).status == "open"


def _row(Session, book: _Book, conflict_id: str) -> tuple[str, list[str], str]:
    """The conflict's status, result refs and decided option."""
    with Session() as session:
        row = WorldConflictRepository(session).get(book.project_id, conflict_id)
        return row.status, json.loads(row.result_refs_json), json.loads(row.decision_json).get("option", "")


def _runtime(Session, book: _Book) -> BookStateRuntime:
    with Session() as session:
        return BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)


def _delta_id(book: _Book, conflict_id: str) -> str:
    return f"world-tick:{book.project_id}:conflict-{conflict_id}"


def _resolution_deltas(Session, book: _Book, conflict_id: str) -> list[GraphDelta]:
    with Session() as session:
        deltas = BookStateRepository(session).list_graph_deltas(book.project_id, after_chapter=0, through_chapter=1)
    return [delta for delta in deltas if delta.metadata.get("tick_id") == f"conflict-{conflict_id}"]


def _world_engine_delta_count(Session, book: _Book) -> int:
    with Session() as session:
        return len(session.scalars(select(GraphDeltaRow.id).where(
            GraphDeltaRow.project_id == book.project_id, GraphDeltaRow.source_type == WORLD_ENGINE_SOURCE_TYPE,
        )).all())


def _resolved_events(Session, book: _Book, conflict_id: str) -> list[tuple[str, dict[str, Any]]]:
    with Session() as session:
        rows = session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == book.project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_RESOLVED,
            DecisionEvent.related_object_id == conflict_id,
        )).all()
        return [(row.actor_type, json.loads(row.payload_json)) for row in rows]


def _resolved_reasons(Session, book: _Book, conflict_id: str) -> list[str]:
    with Session() as session:
        return list(session.scalars(select(DecisionEvent.reason).where(
            DecisionEvent.project_id == book.project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_RESOLVED,
            DecisionEvent.related_object_id == conflict_id,
        )))


def _plan(Session, plan_id: str) -> tuple[list[str], dict[str, Any]]:
    with Session() as session:
        plan = session.get(ChapterPlan, plan_id)
        return load_goals_json(plan.goals_json), json.loads(plan.experience_plan_json)


def _candidates(Session, book: _Book, conflict_id: str, option: str, *, reroll_count: int = 0, policy=POLICY):
    runtime = _runtime(Session, book)
    with Session() as session:
        row = WorldConflictRepository(session).get(book.project_id, conflict_id)
        return option_candidates(
            row, _decision(option), runtime=runtime, policy=policy, reroll_count=reroll_count, now=book.now,
        )


def _event_nodes(runtime: BookStateRuntime, candidate_id: str) -> list[str]:
    return [
        node.id for node in runtime.world.nodes_by_id.values()
        if str(node.node_type) == "event" and node.metadata.get("candidate_id") == candidate_id
    ]


# veto_world


def test_veto_marks_intent_vetoed_and_reschedules_actor_turn_within_reroll_cap(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    hero = book.world.hero
    # An earlier tick left news in transit; a resolution delta must carry the queue forward.
    news = NewsItem(fact_ref=book.world.fact("fact-public"), observer_ref=book.world.faction, arrive_at=book.now + 30)
    tick = _built(Session, _context(book.world, book.chapter), [], tick_id="tick-1", news=[news])
    _commit_tick(Session, book.world, tick, tick_id="tick-1")
    policy = _policy(max_rerolls=1)
    first = _conflict(
        Session, book, proposed_change=_slip(book, book.onstage), options=["veto_world", "plot_responds", "amend_plan"],
        violated="onstage_slip", impact="minor", intent_ref=book.onstage, actor_ref=hero,
    )
    second = _conflict(
        Session, book, proposed_change=_slip(book, book.later), options=["veto_world", "plot_responds", "amend_plan"],
        violated="onstage_slip", impact="minor", intent_ref=book.later, actor_ref=hero,
    )
    turn_before = _runtime(Session, book).world.get_state(hero).get("next_turn_at")

    def shape(candidates: list[TickCandidate]) -> list[tuple[str, str, dict, dict, dict]]:
        return [
            (item.candidate_id, item.origin, item.intent_status_changes, item.next_turn_at, item.slip_updates)
            for item in candidates
        ]

    # Within the cap the actor acts again at the latest chapter's end; at the cap it does not.
    assert shape(_candidates(Session, book, first, "veto_world", policy=policy)) == [
        (f"conflict:{first}:veto", "conflict_resolution", {book.onstage: "vetoed"}, {hero: book.now}, {}),
    ]
    assert shape(_candidates(Session, book, first, "veto_world", reroll_count=1, policy=policy)) == [
        (f"conflict:{first}:veto", "conflict_resolution", {book.onstage: "vetoed"}, {}, {}),
    ]

    result = _resolve(Session, book, first, "veto_world", policy=policy)

    assert result == ConflictResolutionResult(
        graph_delta_ids=(_delta_id(book, first),), obligation_id="", plan_patch_id="",
    )
    [delta] = _resolution_deltas(Session, book, first)
    assert delta.id == _delta_id(book, first)
    # Summaries carry the conflict's own text; the option is structured metadata only.
    assert delta.summary == SUMMARY
    assert {patch.reason for patch in delta.node_patches} == {SUMMARY}
    assert (delta.metadata["conflict_id"], delta.metadata["conflict_option"]) == (first, "veto_world")
    # The held slip is not committed: only the veto's patches land.
    assert _patches(delta) == [
        (book.onstage, "set", "state.status", "planned", "vetoed"),
        (hero, "set", "state.next_turn_at", turn_before, book.now),
    ]
    runtime = _runtime(Session, book)
    assert (runtime.world.get_state(book.onstage)["status"], runtime.world.get_state(book.onstage)["slip_count"]) == (
        "vetoed", 0,
    )
    assert runtime.world.get_state(hero)["next_turn_at"] == book.now
    assert delta.metadata["news_in_transit"] == [news.model_dump(mode="json")]
    with Session() as session:
        assert latest_news_in_transit(session, book.project_id) == [news]
    assert _row(Session, book, first) == ("resolved", [_delta_id(book, first)], "veto_world")
    [(actor_type, payload)] = _resolved_events(Session, book, first)
    assert actor_type == "manual_ui"
    assert _resolved_reasons(Session, book, first) == ["rationale-1"]
    assert {key: payload[key] for key in ("conflict_id", "option", "decided_by", "graph_delta_ids")} == {
        "conflict_id": first, "option": "veto_world", "decided_by": "human",
        "graph_delta_ids": [_delta_id(book, first)],
    }

    # The resolved veto is the actor's reroll record: its one reroll is spent, so the second veto only stops the intent.
    _resolve(Session, book, second, "veto_world", policy=policy)

    [second_delta] = _resolution_deltas(Session, book, second)
    assert _patches(second_delta) == [(book.later, "set", "state.status", "planned", "vetoed")]
    # A settled intent keeps its status, so a spent veto has nothing left to change.
    assert _candidates(Session, book, second, "veto_world", reroll_count=1, policy=policy) == []
    with Session() as session:
        assert WorldConflictRepository(session).reroll_count(book.project_id, actor_ref=hero) == 2
    engine.dispose()


def test_replay_cas_veto_acknowledges_and_an_unreadable_held_change_cannot_commit(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    replay = _conflict(
        Session, book, proposed_change={"graph_delta_id": "delta-x", "tick_id": "tick-x"}, options=["veto_world"],
        source="historical_rewrite", violated="replay_cas",
    )

    _resolve_failing(Session, book, replay, "plot_responds", ValueError)
    result = _resolve(Session, book, replay, "veto_world")

    # A rewrite's veto only acknowledges what the rewrite already dropped.
    assert result == ConflictResolutionResult(graph_delta_ids=(), obligation_id="", plan_patch_id="")
    assert _world_engine_delta_count(Session, book) == 0
    assert _row(Session, book, replay) == ("resolved", [], "veto_world")
    assert len(_resolved_events(Session, book, replay)) == 1

    unreadable = _conflict(Session, book, proposed_change={"patches": []},
                           options=["veto_world", "plot_responds"], intent_ref=book.later)
    _resolve_failing(Session, book, unreadable, "plot_responds", ConflictOptionUnavailable)
    veto = _resolve(Session, book, unreadable, "veto_world")
    assert _row(Session, book, unreadable) == ("resolved", list(veto.graph_delta_ids), "veto_world")
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "vetoed"
    engine.dispose()


# accepting the change


def test_accept_with_obligation_commits_change_and_records_obligation(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))

    result = _resolve(Session, book, conflict_id, "accept_with_obligation")

    delta_id = _delta_id(book, conflict_id)
    assert result.graph_delta_ids == (delta_id,)
    assert result.obligation_id and result.plan_patch_id
    runtime = _runtime(Session, book)
    assert runtime.world.get_state(book.later)["status"] == "resolved"
    assert len(_event_nodes(runtime, "adjudication:held")) == 1
    # The accepted event's news spreads in the same delta: the hero and the co-located faction
    # witness it now, and it reaches the rival at loc 2 after the two-hour road.
    fact_ref = tick_event_fact_ref(book.project_id, f"conflict-{conflict_id}", "adjudication:held", 0)
    [delta] = _resolution_deltas(Session, book, conflict_id)
    assert sorted((patch.observer_id, patch.new_value) for patch in delta.cognition_patches) == sorted([
        (book.world.hero, {fact_ref: "knows"}), (book.world.faction, {fact_ref: "knows"}),
    ])
    assert delta.metadata["news_in_transit"] == [
        NewsItem(fact_ref=fact_ref, observer_ref=book.world.rival, arrive_at=book.now + 120).model_dump(mode="json"),
    ]
    faction = runtime.cognition_by_observer[("faction", book.world.faction)]
    assert faction.knowledge_by_ref[fact_ref] == "knows"
    with Session() as session:
        obligation = session.get(NarrativeObligationRow, result.obligation_id)
        # The writer-visible texts come from the change's public event, not the conflict summary.
        response = plot_response_goal("event-held")
        assert (
            obligation.obligation_type, obligation.summary, obligation.payoff_test, obligation.deadline_chapter,
            obligation.origin_chapter_number, obligation.status,
        ) == ("world_change_response", response, response, 7, 1, "planned")
        patch = session.get(NarrativePlanPatchRow, result.plan_patch_id)
        assert (patch.target_plan_id, patch.applied, json.loads(patch.affected_chapters_json)) == (
            book.plans[2], True, [2],
        )
        assert json.loads(obligation.linked_plan_patch_ids_json) == [result.plan_patch_id]
    goals, _experience = _plan(Session, book.plans[2])
    assert any(result.obligation_id in goal and "event-held" in goal for goal in goals)
    assert not any(SUMMARY in goal for goal in goals)
    assert _plan(Session, book.plans[3])[0] == ["goal-3"]
    assert _row(Session, book, conflict_id) == (
        "resolved", [delta_id, result.obligation_id, result.plan_patch_id], "accept_with_obligation",
    )
    [(_actor_type, payload)] = _resolved_events(Session, book, conflict_id)
    assert {key: payload[key] for key in ("option", "graph_delta_ids", "obligation_id", "plan_patch_id")} == {
        "option": "accept_with_obligation", "graph_delta_ids": [delta_id],
        "obligation_id": result.obligation_id, "plan_patch_id": result.plan_patch_id,
    }
    engine.dispose()


def test_obligation_budget_rejection_keeps_conflict_open(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    with Session.begin() as session:
        # Three open P1 obligations fill the book's budget.
        for index in range(3):
            session.add(NarrativeObligationRow(
                id=f"existing-{book.project_id}-{index}", project_id=book.project_id, origin_chapter_number=1,
                obligation_type="motivation_gap", priority="P1", status="planned", summary="summary-existing",
                hardness="design_debt", deadline_chapter=5, payoff_test="payoff-existing",
            ))
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))

    _resolve_failing(
        Session, book, conflict_id, "accept_with_obligation", ConflictOptionUnavailable,
        match="obligation_budget_exceeded",
    )

    # The change was built and committed inside the resolution, then rolled back with it.
    assert _resolution_deltas(Session, book, conflict_id) == []
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "planned"
    with Session() as session:
        assert session.scalars(select(NarrativeObligationRow.id).where(
            NarrativeObligationRow.project_id == book.project_id,
            NarrativeObligationRow.obligation_type == "world_change_response",
        )).all() == []
    assert _plan(Session, book.plans[2])[0] == ["goal-2"]
    assert _resolved_events(Session, book, conflict_id) == []
    engine.dispose()


def test_obligation_deadline_stops_at_the_book_target(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))

    def target(total: int) -> None:
        with Session.begin() as session:
            session.get(Project, book.project_id).target_total_chapters = total

    # No chapter follows chapter 1 within a one-chapter book.
    target(1)
    _resolve_failing(Session, book, conflict_id, "accept_with_obligation", ConflictOptionUnavailable)
    assert _resolution_deltas(Session, book, conflict_id) == []

    # Two chapters remain: the obligation falls due at the last one, not six chapters on.
    target(3)
    result = _resolve(Session, book, conflict_id, "accept_with_obligation")

    with Session() as session:
        assert session.get(NarrativeObligationRow, result.obligation_id).deadline_chapter == 3
    engine.dispose()


def test_plot_responds_adds_goal_to_next_unwritten_plan(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    held = _held(book)
    # A held relation change is not attempted, so it emits no builder warning.
    held = held.model_copy(update={"effects": held.effects.model_copy(update={"relation_changes": [
        EffectRelationChange(
            source_ref=book.world.hero, target_ref=book.world.rival, edge_type="ally_of", change="end",
        ),
    ]})})
    conflict_id = _conflict(Session, book, proposed_change=held.model_dump(mode="json"))

    result = _resolve(Session, book, conflict_id, "plot_responds", reason="reason-1")

    assert _resolved_reasons(Session, book, conflict_id) == ["reason-1"]
    with Session() as session:
        row = WorldConflictRepository(session).get(book.project_id, conflict_id)
        assert json.loads(row.decision_json)["reason"] == "reason-1"
    assert result.warnings == ()
    [(_actor_type, payload)] = _resolved_events(Session, book, conflict_id)
    assert payload["warnings"] == [dict(warning) for warning in result.warnings]

    delta_id = _delta_id(book, conflict_id)
    goal = plot_response_goal("event-held")
    assert "event-held" in goal and SUMMARY not in goal and "summary-held" not in goal
    assert result.graph_delta_ids == (delta_id,)
    assert result.obligation_id == ""
    goals, experience = _plan(Session, book.plans[2])
    assert goals == [goal, "goal-2"]
    assert experience["progress_markers"] == [goal]
    assert _plan(Session, book.plans[3]) == (["goal-3"], {})
    with Session() as session:
        patch = session.get(NarrativePlanPatchRow, result.plan_patch_id)
        assert (patch.patch_type, patch.target_scope, patch.target_plan_id, patch.applied) == (
            "world_conflict_response", "chapter", book.plans[2], True,
        )
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "held"
    assert _row(Session, book, conflict_id) == ("resolved", [delta_id, result.plan_patch_id], "plot_responds")
    engine.dispose()


def test_plot_responds_without_an_unwritten_plan_is_pending(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    with Session.begin() as session:
        for number in (2, 3):
            session.get(ChapterPlan, book.plans[number]).status = "accepted"
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))

    result = _resolve(Session, book, conflict_id, "plot_responds")

    assert result.graph_delta_ids and result.plan_patch_id == ""
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "held"
    with Session() as session:
        row = WorldConflictRepository(session).get(book.project_id, conflict_id)
        assert row.status == "resolved"
        assert json.loads(row.decision_json)["pending_plan_response"] is True
        assert session.scalars(select(NarrativePlanPatchRow).where(
            NarrativePlanPatchRow.project_id == book.project_id,
        )).all() == []
    engine.dispose()


def test_amend_plan_releases_protections_and_requires_human(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch, protections=lambda world: [
        ("protect-rival", {"ref": world.rival, "kind": "must_be_alive", "until_chapter": 0, "source": "test"}),
        ("protect-hero", {"ref": world.hero, "kind": "must_be_alive", "until_chapter": 0, "source": "test"}),
    ])
    rival, hero = book.world.rival, book.world.hero
    held = TickCandidate(
        candidate_id="adjudication:kill", origin="adjudication", actor_id=hero, summary="summary-kill",
        effects=WorldEffects(entity_changes=[EffectEntityChange(ref=rival, field="alive_status", new_value="dead")]),
        impact_level="major",
    )
    conflict_id = _conflict(
        Session, book, proposed_change=held.model_dump(mode="json"),
        options=["veto_world", "plot_responds", "amend_plan"], violated="protection", impact="major", actor_ref=hero,
    )

    _resolve_failing(Session, book, conflict_id, "amend_plan", ValueError, decided_by="codex")
    assert [
        (item.candidate_id, item.protection_releases) for item in _candidates(Session, book, conflict_id, "amend_plan")
    ] == [("adjudication:kill", []), (f"conflict:{conflict_id}:release", ["protect-rival"])]

    result = _resolve(Session, book, conflict_id, "amend_plan")

    delta_id = _delta_id(book, conflict_id)
    assert result == ConflictResolutionResult(graph_delta_ids=(delta_id,), obligation_id="", plan_patch_id="")
    [delta] = _resolution_deltas(Session, book, conflict_id)
    # The release rides in the same delta as the change, with the protection's old payload status.
    assert [(patch.target_ref, str(patch.op), patch.field_path, patch.old_value, patch.new_value)
            for patch in delta.narrative_patches] == [
        ("review_constraint:protect-rival", "set", "payload.status", None, "released"),
    ]
    runtime = _runtime(Session, book)
    assert runtime.world.get_state(rival)["alive_status"] == "dead"
    assert runtime.narrative.nodes_by_id["protect-rival"].payload["status"] == "released"
    assert runtime.narrative.nodes_by_id["protect-hero"].payload["until_chapter"] == 0
    assert check_narrative_boundaries(runtime, held, next_chapter=2) == []
    assert _row(Session, book, conflict_id) == ("resolved", [delta_id], "amend_plan")
    engine.dispose()


# guards


def test_resolving_twice_raises_and_applies_once(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))

    first = _resolve(Session, book, conflict_id, "plot_responds")
    _resolve_failing_resolved(Session, book, conflict_id, "plot_responds")
    _resolve_failing_resolved(Session, book, conflict_id, "veto_world")

    assert [delta.id for delta in _resolution_deltas(Session, book, conflict_id)] == list(first.graph_delta_ids)
    assert len(_resolved_events(Session, book, conflict_id)) == 1
    assert _plan(Session, book.plans[2])[0] == [plot_response_goal("event-held"), "goal-2"]
    with Session() as session:
        assert len(session.scalars(select(NarrativePlanPatchRow.id).where(
            NarrativePlanPatchRow.project_id == book.project_id,
            NarrativePlanPatchRow.patch_type == "world_conflict_response",
        )).all()) == 1
    assert _row(Session, book, conflict_id)[0] == "resolved"
    engine.dispose()


def _resolve_failing_resolved(Session, book: _Book, conflict_id: str, option: str) -> None:
    with Session.begin() as session:
        with pytest.raises(ConflictNotOpen):
            _service(session).resolve(project_id=book.project_id, conflict_id=conflict_id, decision=_decision(option))


class _RacingAdmission(CanonAdmissionService):
    """Another canon writer commits between the resolution's build and its commit."""

    def __init__(self, race: Callable[[], None]) -> None:
        super().__init__()
        self._race = race

    def commit_world_tick(self, **kwargs: Any):
        self._race()
        return super().commit_world_tick(**kwargs)


class _StaleAdmission(CanonAdmissionService):
    """Canon admission finds a patch's old_value no longer current."""

    def commit_world_tick(self, **kwargs: Any):
        raise CanonStaleVersion("old_value mismatch")


def test_stale_held_change_keeps_conflict_open(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))

    _resolve_failing(Session, book, conflict_id, "plot_responds", ConflictStale, admission=_StaleAdmission())

    assert _row(Session, book, conflict_id) == ("open", [], "")
    assert _resolution_deltas(Session, book, conflict_id) == []
    assert _resolved_events(Session, book, conflict_id) == []
    assert _plan(Session, book.plans[2])[0] == ["goal-2"]
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "planned"
    engine.dispose()


def test_rewrite_chapter_unavailable_in_p3(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(
        Session, book, proposed_change=_held(book).model_dump(mode="json"), options=["veto_world", "rewrite_chapter"],
    )

    _resolve_failing(Session, book, conflict_id, "rewrite_chapter", ConflictOptionUnavailable)
    # An option the conflict does not offer is a bad request, whatever the option.
    _resolve_failing(Session, book, conflict_id, "plot_responds", ValueError)
    with pytest.raises(ConflictOptionUnavailable):
        _candidates(Session, book, conflict_id, "rewrite_chapter")

    assert _row(Session, book, conflict_id) == ("open", [], "")
    assert _world_engine_delta_count(Session, book) == 0
    engine.dispose()


def test_plot_responds_cannot_commit_a_change_that_breaks_an_active_protection(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch, protections=lambda world: [
        ("protect-rival", {"ref": world.rival, "kind": "must_be_alive", "until_chapter": 0, "source": "test"}),
    ])
    held = TickCandidate(
        candidate_id="adjudication:kill", origin="adjudication", actor_id=book.world.hero,
        intent_id=book.later, summary="summary-kill",
        effects=WorldEffects(entity_changes=[
            EffectEntityChange(ref=book.world.rival, field="alive_status", new_value="dead"),
        ]),
        impact_level="minor",
    )
    # An older row may still offer every option; the protection keeps the commit options closed anyway.
    conflict_id = _conflict(Session, book, proposed_change=held.model_dump(mode="json"), violated="protection")

    _resolve_failing(Session, book, conflict_id, "plot_responds", ConflictOptionUnavailable)
    _resolve_failing(Session, book, conflict_id, "accept_with_obligation", ConflictOptionUnavailable)

    assert _row(Session, book, conflict_id) == ("open", [], "")
    assert _runtime(Session, book).world.get_state(book.world.rival).get("alive_status") != "dead"
    assert _resolution_deltas(Session, book, conflict_id) == []
    engine.dispose()


def test_accepting_a_change_to_a_settled_intent_is_stale(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    vetoed = _conflict(
        Session, book, proposed_change=_slip(book, book.later), violated="onstage_slip", impact="minor",
        intent_ref=book.later, actor_ref=book.world.hero,
    )
    accepted = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))
    _resolve(Session, book, vetoed, "veto_world")

    # The held change would resolve the intent the veto just settled.
    _resolve_failing(Session, book, accepted, "accept_with_obligation", ConflictStale)

    assert _row(Session, book, accepted) == ("open", [], "")
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "vetoed"
    assert _resolution_deltas(Session, book, accepted) == []
    engine.dispose()


def test_veto_reroll_cap_counts_only_recent_vetoes(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    hero = book.world.hero
    policy = _policy(max_rerolls=1)
    old = _conflict(
        Session, book, proposed_change=_slip(book, book.onstage), violated="onstage_slip", impact="minor",
        intent_ref=book.onstage, actor_ref=hero,
    )
    _resolve(Session, book, old, "veto_world", policy=policy)
    for number in (2, 3):
        _commit(Session, book.world, _delta(Session, book.world, number, _time(1, "日")))
    recent = _conflict(
        Session, book, proposed_change=_slip(book, book.later), violated="onstage_slip", impact="minor",
        intent_ref=book.later, actor_ref=hero, detected_after_chapter=3,
    )
    with Session() as session:
        chapter_three_end = session.scalar(select(GraphDeltaRow.story_time_end).where(
            GraphDeltaRow.project_id == book.project_id, GraphDeltaRow.chapter_number == 3,
        ))

    _resolve(Session, book, recent, "veto_world", policy=policy)

    # The chapter-1 veto is outside the window (chapters 2 to 3), so the actor rerolls again.
    with Session() as session:
        deltas = BookStateRepository(session).list_graph_deltas(book.project_id, after_chapter=2, through_chapter=3)
    [delta] = [item for item in deltas if item.metadata.get("tick_id") == f"conflict-{recent}"]
    assert [(node_id, field_path, new) for node_id, _op, field_path, _old, new in _patches(delta)] == [
        (book.later, "state.status", "vetoed"), (hero, "state.next_turn_at", chapter_three_end),
    ]
    engine.dispose()


def test_no_canon_writer_commits_under_a_resolution(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    # An earlier tick left news in transit: the resolution must carry exactly that queue forward.
    news = NewsItem(fact_ref=book.world.fact("fact-public"), observer_ref=book.world.faction, arrive_at=book.now + 30)
    _commit_tick(Session, book.world, _built(Session, _context(book.world, book.chapter), [], tick_id="tick-1",
                                             news=[news]), tick_id="tick-1")
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))
    blocked: list[bool] = []

    def race() -> None:
        # Another canon writer, as a chapter commit or a second resolve would, reaches for the project row.
        with Session() as other:
            try:
                other.execute(select(Project.id).where(Project.id == book.project_id).with_for_update(
                    key_share=True, nowait=True,
                ))
            except OperationalError:
                blocked.append(True)
            else:
                blocked.append(False)
            other.rollback()

    with Session.begin() as session:
        _service(session, admission=_RacingAdmission(race)).resolve(
            project_id=book.project_id, conflict_id=conflict_id, decision=_decision("plot_responds"),
        )

    # The resolution held the project row from the start, so the writer would have waited for it.
    assert blocked == [True]
    assert _row(Session, book, conflict_id)[0] == "resolved"
    [delta] = _resolution_deltas(Session, book, conflict_id)
    assert news.model_dump(mode="json") in delta.metadata["news_in_transit"]
    engine.dispose()


class _RefusingAdmission(CanonAdmissionService):
    def commit_world_tick(self, **kwargs: Any):
        raise CanonWriteFailure("compiler refused the tick")


def test_other_canon_write_failures_propagate(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))

    _resolve_failing(
        Session, book, conflict_id, "plot_responds", CanonWriteFailure, admission=_RefusingAdmission(),
        match="compiler refused",
    )
    engine.dispose()


def test_missing_conflict_raises_lookup_error(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)

    with Session.begin() as session:
        with pytest.raises(LookupError):
            _service(session).resolve(
                project_id=book.project_id, conflict_id="missing-conflict", decision=_decision("veto_world"),
            )
    engine.dispose()


def test_veto_with_an_unknown_actor_only_stops_the_intent(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(
        Session, book, proposed_change=_slip(book, book.onstage), violated="onstage_slip", impact="minor",
        intent_ref=book.onstage, actor_ref="actor-not-in-book-state",
    )

    _resolve(Session, book, conflict_id, "veto_world")

    [delta] = _resolution_deltas(Session, book, conflict_id)
    assert _patches(delta) == [(book.onstage, "set", "state.status", "planned", "vetoed")]
    engine.dispose()


def test_builder_releases_only_review_constraints_and_records_an_absent_status(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch, protections=lambda world: [
        ("protect-open", {"ref": world.rival, "kind": "must_be_alive", "source": "test"}),
    ])
    context = _context(book.world, book.chapter)

    def release(ref: str) -> TickCandidate:
        return TickCandidate(
            candidate_id="c-release", origin="conflict_resolution", summary=SUMMARY, effects=WorldEffects(),
            protection_releases=[ref],
        )

    unknown = _build(Session, context, [release("not-a-protection")], tick_id="tick-release")
    assert unknown.delta is None
    assert _issues(unknown) == [("error", "unresolved_ref", "c-release")]
    built = _built(Session, context, [release("protect-open")], tick_id="tick-release")
    assert [(patch.target_ref, patch.field_path, patch.old_value, patch.new_value)
            for patch in built.narrative_patches] == [
        ("review_constraint:protect-open", "payload.status", None, "released"),
    ]
    engine.dispose()


def test_releasing_an_open_protection_rewinds_to_open(monkeypatch) -> None:
    engine, Session = _session_factory()
    # An open protection that leaves until_chapter out altogether.
    book = _book(Session, monkeypatch, protections=lambda world: [
        ("protect-open", {"ref": world.rival, "kind": "must_be_alive", "source": "test"}),
    ])
    held = TickCandidate(
        candidate_id="adjudication:kill", origin="adjudication", actor_id=book.world.hero, summary="summary-kill",
        effects=WorldEffects(entity_changes=[
            EffectEntityChange(ref=book.world.rival, field="alive_status", new_value="dead"),
        ]),
        impact_level="major",
    )
    conflict_id = _conflict(
        Session, book, proposed_change=held.model_dump(mode="json"), options=["veto_world", "amend_plan"],
        violated="protection", impact="major", actor_ref=book.world.hero,
    )

    # The release records the absent status as None, and canon admission's CAS accepts that.
    _resolve(Session, book, conflict_id, "amend_plan")

    [delta] = _resolution_deltas(Session, book, conflict_id)
    [release] = delta.narrative_patches
    assert (release.field_path, release.old_value, release.new_value) == ("payload.status", None, "released")
    released = _runtime(Session, book)
    assert released.narrative.nodes_by_id["protect-open"].payload["status"] == "released"
    assert check_narrative_boundaries(released, held, next_chapter=2) == []

    # Rewind chapter 1 without the release, as a rewrite that drops its delta does.
    with Session.begin() as session:
        repository = BookStateRepository(session)
        repository.invalidate_project_range(book.project_id, from_chapter=1, through_chapter=1)
        repository.retire_chapter_deltas(book.project_id, 1, delta_ids=[delta.id])
    rewound = _runtime(Session, book)
    assert "status" not in rewound.narrative.nodes_by_id["protect-open"].payload
    assert "until_chapter" not in rewound.narrative.nodes_by_id["protect-open"].payload
    assert [v.constraint_node_id for v in check_narrative_boundaries(rewound, held, next_chapter=2)] == [
        "protect-open",
    ]
    # The release replays onto the rewound canon: its old_value is the open protection's.
    with Session() as session:
        assert BookStateCompiler(session).cas_mismatches(rewound, delta) == []
        closed = rewound.narrative.nodes_by_id["protect-open"]
        rewound.narrative.nodes_by_id["protect-open"] = closed.model_copy(
            update={"payload": {**closed.payload, "status": "released"}},
        )
        assert [label for label, _patch, _current in BookStateCompiler(session).cas_mismatches(rewound, delta)] == [
            "narrative:review_constraint:protect-open:payload.status",
        ]
    engine.dispose()


# writer-visible text (Ruling P32)


def _plot_texts(Session, book: _Book, result: ConflictResolutionResult) -> str:
    """Everything the writer may read of a resolution: plan goals and markers, obligation and plan patch."""
    goals, experience = _plan(Session, book.plans[2])
    with Session() as session:
        texts = [*goals, json.dumps(experience, ensure_ascii=False)]
        if result.obligation_id:
            obligation = session.get(NarrativeObligationRow, result.obligation_id)
            texts.extend([obligation.summary, obligation.payoff_test])
        patch = session.get(NarrativePlanPatchRow, result.plan_patch_id)
        texts.extend([
            patch.writer_context_injections_json, patch.new_contract_json, patch.reviewer_context_injections_json,
            patch.expected_resolution_tests_json,
        ])
    return "\n".join(texts)


@pytest.mark.parametrize("option", ["plot_responds", "accept_with_obligation"])
def test_a_secret_change_reaches_the_writer_only_as_a_generic_response(monkeypatch, option) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    secret = _held(book).model_copy(update={
        "summary": "summary-secret-truth",
        "effects": WorldEffects(events=[EffectEvent(
            summary="event-secret", participant_refs=[book.world.hero], location_id=book.world.loc(1),
            visibility="secret", at=book.now,
        )]),
    })
    conflict_id = _conflict(Session, book, proposed_change=secret.model_dump(mode="json"))

    result = _resolve(Session, book, conflict_id, option)

    texts = _plot_texts(Session, book, result)
    for hidden in ("summary-secret-truth", "event-secret", SUMMARY):
        assert hidden not in texts
    assert PLOT_RESPONSE_GENERIC_GOAL in texts
    if option == "plot_responds":
        assert _plan(Session, book.plans[2])[0] == [PLOT_RESPONSE_GENERIC_GOAL, "goal-2"]
    engine.dispose()


def test_a_public_event_is_all_the_writer_reads_of_a_mixed_change(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    held = _held(book)
    mixed = held.model_copy(update={"effects": held.effects.model_copy(update={"events": [
        *held.effects.events,
        EffectEvent(summary="event-secret", participant_refs=[book.world.hero], location_id=book.world.loc(1),
                    visibility="secret", at=book.now),
    ]})})
    conflict_id = _conflict(Session, book, proposed_change=mixed.model_dump(mode="json"))

    result = _resolve(Session, book, conflict_id, "plot_responds")

    assert _plan(Session, book.plans[2])[0] == [plot_response_goal("event-held"), "goal-2"]
    assert "event-secret" not in _plot_texts(Session, book, result)
    engine.dispose()


def test_an_accepted_onstage_slip_asks_the_next_chapter_to_stage_it(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(
        Session, book, proposed_change=_slip(book, book.onstage), violated="onstage_slip", impact="minor",
        intent_ref=book.onstage, actor_ref=book.world.hero,
    )

    result = _resolve(Session, book, conflict_id, "plot_responds")

    # Nothing has happened yet: the goal stages the intent instead of answering a change.
    assert _plan(Session, book.plans[2])[0] == [ONSTAGE_SLIP_RESPONSE_GOAL, "goal-2"]
    assert "summary-slip" not in _plot_texts(Session, book, result)
    engine.dispose()


# Ruling P30: the resolution's changes are checked in the latest chapter's window


def test_a_held_movement_within_the_chapter_still_resolves(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    start = book.chapter.story_time_start
    walk = TickCandidate(
        candidate_id="adjudication:walk", origin="adjudication", actor_id=book.world.hero, intent_id=book.later,
        summary="summary-walk", effects=WorldEffects(movements=[EffectMovement(
            entity_ref=book.world.hero, to_location_id=book.world.loc(2), travel_mode_id="mode-walk",
            depart_at=start + 60, arrive_at=start + 180,
        )]),
        intent_status_changes={book.later: "resolved"}, impact_level="major",
    )
    conflict_id = _conflict(Session, book, proposed_change=walk.model_dump(mode="json"))

    result = _resolve(Session, book, conflict_id, "accept_with_obligation")

    assert result.graph_delta_ids == (_delta_id(book, conflict_id),)
    assert _runtime(Session, book).world.get_state(book.world.hero)["location_id"] == book.world.loc(2)
    engine.dispose()


@pytest.mark.parametrize("onstage_source", ["plan", "pov", "event", "movement"])
def test_http_accept_refuses_a_change_touching_latest_chapter_onstage(monkeypatch, onstage_source):
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    hero = book.world.hero
    with Session.begin() as session:
        plan = session.get(ChapterPlan, book.plans[1])
        if onstage_source == "plan":
            plan.onstage_refs_json = json.dumps([hero])
        elif onstage_source == "pov":
            plan.pov_ref = hero
    if onstage_source in {"event", "movement"}:
        # Exercise the committed chapter channels through the canonical delta fixture.
        from tests.test_world_gate_v6 import _event, _move
        fields = _time(1, "日")
        if onstage_source == "event":
            fields["events"] = [_event([hero])]
        else:
            fields["movements"] = [_move(hero, book.world.loc(1), book.world.loc(2))]
        chapter = _delta(Session, book.world, 2, fields)
        _commit(Session, book.world, chapter)
    held = _held(book).model_copy(update={"effects": WorldEffects(entity_changes=[
        EffectEntityChange(ref=hero, field="alive_status", new_value="dead"),
    ])})
    conflict_id = _conflict(Session, book, proposed_change=held.model_dump(mode="json"))
    with pytest.raises(ConflictStale, match="touches_onstage_entity"):
        _resolve(Session, book, conflict_id, "accept_with_obligation")
    with Session() as session:
        assert WorldConflictRepository(session).get(book.project_id, conflict_id).status == "open"
        latest = BookStateRepository(session).latest_canon_chapter(book.project_id)
        runtime = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=latest)
        assert runtime.world.get_state(hero)["alive_status"] == "alive"
        assert runtime.world.get_state(book.later)["status"] == "planned"
    engine.dispose()


def test_veto_reroll_window_starts_exactly_max_rerolls_chapters_back(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    hero = book.world.hero
    policy = _policy(max_rerolls=1)
    for number in (2, 3):
        _commit(Session, book.world, _delta(Session, book.world, number, _time(1, "日")))

    def rerolls_after_a_veto_detected_at(chapter: int) -> bool:
        """Whether vetoing the hero now lets it act again, given one earlier veto detected after ``chapter``."""
        earlier = _conflict(
            Session, book, proposed_change=_slip(book, book.onstage), violated="onstage_slip", impact="minor",
            actor_ref=hero, detected_after_chapter=chapter,
        )
        with Session.begin() as session:
            repository = WorldConflictRepository(session)
            repository.mark_resolved(repository.get(book.project_id, earlier, for_update=True),
                                     decision=_decision("veto_world"), result_refs=[])
        # Without an intent to stop, the veto changes the world only by giving the actor its turn back.
        fresh = _conflict(
            Session, book, proposed_change=_slip(book, book.later), violated="onstage_slip", impact="minor",
            actor_ref=hero, detected_after_chapter=3,
        )
        result = _resolve(Session, book, fresh, "veto_world", policy=policy)
        with Session.begin() as session:
            # Each probe starts again from no counted vetoes.
            for conflict_id in (earlier, fresh):
                session.delete(WorldConflictRepository(session).get(book.project_id, conflict_id))
        return bool(result.graph_delta_ids)

    # The latest chapter is 3 and max_rerolls 1: a veto detected after chapter 2 (3 - 1) counts and spends
    # the one reroll; one detected after chapter 1 is outside the window.
    assert rerolls_after_a_veto_detected_at(1) is True
    assert rerolls_after_a_veto_detected_at(2) is False
    engine.dispose()


# amend_plan through PlanAmender (P4a)


PLAN_CONFLICT_SUMMARY = "summary-plan-conflict"
_FEASIBLE = {3: {"milestone_ids": ["milestone-1"]}}


def _band_world(Session):
    """The band planner's book with chapters 1-4 planned as band plan version 1."""
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import _band, _book as band_book, _plan_band, _planner

    world, arc_id, sheet_id = band_book(Session)
    first = _plan_band(Session, _planner(StubLLM(_band(world, range(1, 5), _FEASIBLE))), world, arc_id, sheet_id)
    return world, arc_id, first.band_plan_id


def _plan_conflict(Session, world, *, violated: str = "feasibility", band=(1, 4), band_plan_id=None,
                   source: str = "plan_feasibility", issues=()) -> str:
    with Session.begin() as session:
        row = WorldConflictRepository(session).create(ConflictDraft(
            project_id=world.project_id, source=source, detected_after_chapter=0, story_time=1000,
            involved_refs=[], violated=violated, impact_level="major", summary=PLAN_CONFLICT_SUMMARY,
            proposed_change={"kind": "plan_issues", "band": list(band), "band_plan_id": band_plan_id,
                             "issues": list(issues)},
            blocks_from_chapter=band[0],
        ), required_decider="human", options=("amend_plan",))
        return row.id


def _amender(band_client, sheet_client):
    from forwin.maintenance.post_canon import save_world_prompt_trace
    from forwin.planning.amend import PlanAmender
    from forwin.planning.beat_sheet.service import ArcBeatSheetService
    from forwin.planning.feasibility import FeasibilityAuditor
    from tests.test_band_planner import _planner

    return PlanAmender(_planner(band_client, FeasibilityAuditor()),
                       ArcBeatSheetService(sheet_client, save_trace=save_world_prompt_trace))


def _amend(Session, project_id: str, conflict_id: str, amender, *, decided_by: str = "human"):
    # A plain session: the amender commits between its model calls.
    with Session() as session:
        result = ConflictResolutionService(
            session, admission=CanonAdmissionService(), policy=POLICY, plan_amender=amender,
        ).resolve(project_id=project_id, conflict_id=conflict_id, decision=_decision("amend_plan", decided_by))
        session.commit()
        return result


def _unlocked(Session, project_id: str, conflict_id: str):
    """A model-call hook asserting that nobody holds the project row or the conflict row."""
    def check() -> None:
        with Session.begin() as other:
            other.execute(select(Project.id).where(Project.id == project_id).with_for_update(nowait=True))
            other.execute(select(WorldPlanConflictRow.id).where(WorldPlanConflictRow.id == conflict_id)
                          .with_for_update(nowait=True))
    return check


def _conflict_row(Session, project_id: str, conflict_id: str) -> tuple[str, list[str]]:
    with Session() as session:
        row = WorldConflictRepository(session).get(project_id, conflict_id)
        return row.status, json.loads(row.result_refs_json)


def test_amend_plan_on_plan_conflict_replans_band_with_rationale() -> None:
    from forwin.models.planning_v6 import BandPlanRow
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import HookedLLM, _band

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _band_world(Session)
    with Session.begin() as session:
        # The amendment ignores the replan cap once.
        session.get(BandPlanRow, band_plan_id).replan_count = POLICY.planning.max_replans_per_band
    conflict_id = _plan_conflict(Session, world, band_plan_id=band_plan_id)
    band_client = HookedLLM(_unlocked(Session, world.project_id, conflict_id), _band(world, range(1, 5), _FEASIBLE))
    sheet_client = StubLLM()

    result = _amend(Session, world.project_id, conflict_id, _amender(band_client, sheet_client))

    assert band_client.payload(0)["notes"] == ["rationale-1"]
    assert band_client.payload(0)["chapters_to_plan"] == [1, 2, 3, 4]
    assert sheet_client.calls == []
    assert (result.graph_delta_ids, result.beat_sheet_id) == ((), "")
    with Session() as session:
        replanned = session.get(BandPlanRow, result.band_plan_id)
        assert (replanned.version, replanned.status, replanned.replan_count) == (
            2, "active", POLICY.planning.max_replans_per_band + 1,
        )
        assert session.get(BandPlanRow, band_plan_id).status == "superseded"
    assert _conflict_row(Session, world.project_id, conflict_id) == ("resolved", [f"band_plan:{result.band_plan_id}"])
    with Session() as session:
        (event,) = session.scalars(select(DecisionEvent).where(
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_RESOLVED,
            DecisionEvent.related_object_id == conflict_id,
        )).all()
        payload = json.loads(event.payload_json)
        assert (payload["option"], payload["band_plan_id"], payload["beat_sheet_id"]) == (
            "amend_plan", result.band_plan_id, "",
        )
    engine.dispose()


def test_amend_plan_on_plan_conflict_needs_a_human_and_an_amender() -> None:
    from tests.test_arc_beat_sheet import StubLLM

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _band_world(Session)
    conflict_id = _plan_conflict(Session, world, band_plan_id=band_plan_id)

    with Session() as session:
        with pytest.raises(ConflictOptionUnavailable, match="plan amender"):
            _service(session).resolve(project_id=world.project_id, conflict_id=conflict_id,
                                      decision=_decision("amend_plan"))
    with pytest.raises(ValueError, match="Only a human"):
        _amend(Session, world.project_id, conflict_id, _amender(StubLLM(), StubLLM()), decided_by="codex")
    with Session() as session:
        with pytest.raises(ValueError, match="offers"):
            _service(session).resolve(project_id=world.project_id, conflict_id=conflict_id,
                                      decision=_decision("veto_world"))
    assert _conflict_row(Session, world.project_id, conflict_id) == ("open", [])
    engine.dispose()


def test_failed_amendment_keeps_the_conflict_open_and_its_trace() -> None:
    from forwin.models.genesis import PromptTrace
    from tests.test_arc_beat_sheet import StubLLM

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _band_world(Session)
    conflict_id = _plan_conflict(Session, world, band_plan_id=band_plan_id)
    band_client = StubLLM(RuntimeError("provider down"), RuntimeError("provider down"))

    with pytest.raises(ConflictOptionUnavailable, match="llm_unavailable"):
        _amend(Session, world.project_id, conflict_id, _amender(band_client, StubLLM()))

    assert len(band_client.calls) == 2
    # The failed amendment is recorded on the conflict, which stays open.
    status, refs = _conflict_row(Session, world.project_id, conflict_id)
    assert status == "open" and [ref.split(":")[0] for ref in refs] == ["amend_failed"]
    with Session() as session:
        traces = session.scalars(select(PromptTrace).where(
            PromptTrace.project_id == world.project_id, PromptTrace.stage_key == "plan.band",
        )).all()
        assert len(traces) == 2
    engine.dispose()


def test_amend_plan_on_milestone_conflict_revises_beat_sheet_first() -> None:
    from forwin.models.planning_v6 import BandPlanRow
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from tests.test_arc_beat_sheet import StubLLM, _sheet
    from tests.test_band_planner import HookedLLM, _band

    engine, Session = _session_factory()
    world, arc_id, band_plan_id = _band_world(Session)
    conflict_id = _plan_conflict(Session, world, violated="milestone", band_plan_id=band_plan_id)
    revised = _sheet(world, bands=((1, 4), (5, 8), (9, 12)))
    revised["milestones"][0]["description"] = "revised-milestone"
    sheet_client = StubLLM(revised)
    # The revision's project row lock is released before the band is planned again.
    band_client = HookedLLM(_unlocked(Session, world.project_id, conflict_id), _band(world, range(1, 5)))

    result = _amend(Session, world.project_id, conflict_id, _amender(band_client, sheet_client))

    revision = sheet_client.payload(0)["revision"]
    assert PLAN_CONFLICT_SUMMARY in revision["conflict_summary"] and "rationale-1" in revision["conflict_summary"]
    # The band is planned again from the revised sheet, keeping its band plan.
    payload = band_client.payload(0)
    assert [item["description"] for item in payload["milestones"]] == ["revised-milestone"]
    assert payload["notes"] == ["rationale-1"]
    with Session() as session:
        sheet = ArcBeatSheetRepository(session).active_for_arc(arc_id)
        assert (sheet.id, sheet.version) == (result.beat_sheet_id, 2)
        replanned = session.get(BandPlanRow, result.band_plan_id)
        assert (replanned.version, replanned.beat_sheet_id, replanned.chapter_end) == (2, sheet.id, 4)
    assert _conflict_row(Session, world.project_id, conflict_id) == (
        "resolved", [f"beat_sheet:{result.beat_sheet_id}", f"band_plan:{result.band_plan_id}"],
    )
    engine.dispose()


def test_amendment_plans_the_new_band_when_the_revision_resplits_it() -> None:
    from forwin.models.planning_v6 import BandPlanRow
    from tests.test_arc_beat_sheet import StubLLM, _sheet
    from tests.test_band_planner import _band

    engine, Session = _session_factory()
    world, arc_id, band_plan_id = _band_world(Session)
    issues = [{"code": "exit_state_unaddressed", "severity": "warning", "chapter_number": 4, "ref": world.rival,
               "message": "m"}]
    conflict_id = _plan_conflict(Session, world, band_plan_id=band_plan_id, issues=issues)
    # Chapters 1-4 now sit in the revised sheet's band 1-6.
    sheet_client = StubLLM(_sheet(world, bands=((1, 6), (7, 12))))
    band_client = StubLLM(_band(world, range(1, 7)))

    result = _amend(Session, world.project_id, conflict_id, _amender(band_client, sheet_client))

    assert len(sheet_client.calls) == 1
    assert band_client.payload(0)["band"] == {"band_index": 0, "chapter_start": 1, "chapter_end": 6}
    assert band_client.payload(0)["chapters_to_plan"] == [1, 2, 3, 4, 5, 6]
    with Session() as session:
        planned = session.get(BandPlanRow, result.band_plan_id)
        assert (planned.status, planned.chapter_start, planned.chapter_end) == ("active", 1, 6)
        assert planned.beat_sheet_id == result.beat_sheet_id
    assert _conflict_row(Session, world.project_id, conflict_id)[0] == "resolved"
    engine.dispose()


def test_world_amend_plan_triggers_replan_of_unwritten_band_chapters() -> None:
    from forwin.models.planning_v6 import BandPlanRow
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import HookedLLM, _band

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _band_world(Session)
    held = TickCandidate(
        candidate_id="adjudication:held", origin="adjudication", actor_id=world.hero, summary="summary-held",
        effects=WorldEffects(entity_changes=[EffectEntityChange(ref=world.rival, field="mood", new_value="wary")]),
        impact_level="major",
    )
    with Session.begin() as session:
        conflict_id = WorldConflictRepository(session).create(ConflictDraft(
            project_id=world.project_id, source="world_tick", detected_after_chapter=0, story_time=1000,
            involved_refs=[world.rival], violated="impact_review", impact_level="major", summary=SUMMARY,
            proposed_change=held.model_dump(mode="json"), actor_ref=world.hero, blocks_from_chapter=1,
        ), required_decider="human", options=("veto_world", "plot_responds", "amend_plan")).id
    # The resolution's conflict row and project row locks are released before the replan's model call.
    band_client = HookedLLM(_unlocked(Session, world.project_id, conflict_id), _band(world, range(1, 5), _FEASIBLE))

    result = _amend(Session, world.project_id, conflict_id, _amender(band_client, StubLLM()))

    assert len(result.graph_delta_ids) == 1
    with Session() as session:
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        assert runtime.world.get_state(world.rival)["mood"] == "wary"
    payload = band_client.payload(0)
    assert (payload["chapters_to_plan"], payload["notes"]) == ([1, 2, 3, 4], [SUMMARY])
    with Session() as session:
        assert session.get(BandPlanRow, result.band_plan_id).version == 2
    assert _conflict_row(Session, world.project_id, conflict_id) == (
        "resolved", [*result.graph_delta_ids, f"band_plan:{result.band_plan_id}"],
    )
    engine.dispose()


@pytest.fixture
def pending_side_effect_session():
    """The response ledger needs SQL persistence, but no PostgreSQL canon locks."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from forwin.models.base import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id="pending-project", title="Pending", premise="Pending", genre="test",
                            target_total_chapters=20))
        session.flush()
        row = WorldConflictRepository(session).create(ConflictDraft(
            project_id="pending-project", source="world_tick", detected_after_chapter=1,
            story_time=100, involved_refs=[], violated="impact_review", impact_level="moderate",
            summary="held", proposed_change={}, blocks_from_chapter=2,
        ), required_decider="codex", options=ALL_OPTIONS)
        yield session, row
    engine.dispose()


def test_pending_plot_response_side_effect_is_discoverable(pending_side_effect_session):
    from forwin.world_engine.conflict_resolution import record_option_side_effects

    session, row = pending_side_effect_session
    decision = _decision("plot_responds")
    effects = record_option_side_effects(session, row, decision, latest_chapter=1)
    assert effects.plan_patch_id == effects.obligation_id == ""
    repo = WorldConflictRepository(session)
    repo.mark_resolved(row, decision=decision, result_refs=[], pending_plan_response=effects.pending_plan_response)
    assert json.loads(row.decision_json)["pending_plan_response"] is True
    assert repo.pending_plot_responses(row.project_id) == [row]
    assert session.scalars(select(NarrativePlanPatchRow)).all() == []
    repo.attach_result_refs(row, ["plan_patch:later-response"])
    assert repo.pending_plot_responses(row.project_id) == [row]


def test_accept_with_obligation_without_next_plan_records_obligation_only(pending_side_effect_session):
    from forwin.world_engine.conflict_resolution import record_option_side_effects

    session, row = pending_side_effect_session
    effects = record_option_side_effects(session, row, _decision("accept_with_obligation"), latest_chapter=1)
    assert effects.plan_patch_id == ""
    obligation = session.get(NarrativeObligationRow, effects.obligation_id)
    assert obligation.status == "active"
    assert obligation.deadline_chapter == 7
    assert json.loads(obligation.linked_plan_patch_ids_json) == []
    assert session.scalars(select(NarrativePlanPatchRow)).all() == []

    from forwin.narrative_obligations.repository import NarrativeObligationRepository
    active = NarrativeObligationRepository(session).list_active_for_context(row.project_id, chapter_number=2)
    assert [item.id for item in active] == [effects.obligation_id]


@pytest.mark.parametrize("option", ["plot_responds", "accept_with_obligation"])
def test_pending_response_still_refuses_the_books_last_chapter(pending_side_effect_session, option):
    from forwin.world_engine.conflict_resolution import record_option_side_effects

    session, row = pending_side_effect_session
    with pytest.raises(ConflictOptionUnavailable, match="No chapter follows"):
        record_option_side_effects(session, row, _decision(option), latest_chapter=20)
    assert session.scalars(select(NarrativeObligationRow)).all() == []
    assert session.scalars(select(NarrativePlanPatchRow)).all() == []


@pytest.mark.parametrize("violated", ["milestone", "reveal_schedule"])
@pytest.mark.parametrize("option", ["plot_responds", "accept_with_obligation"])
def test_plan_boundary_defence_rejects_stale_offered_options(pending_side_effect_session, violated, option):
    from tests.test_world_tick_steps import _runtime

    _, row = pending_side_effect_session
    row.violated = violated
    row.proposed_change_json = TickCandidate(candidate_id="held", origin="adjudication", summary="held", effects=WorldEffects()).model_dump_json()
    with pytest.raises(ConflictOptionUnavailable, match="veto.*amend"):
        option_candidates(row, _decision(option), runtime=_runtime(), policy=POLICY, reroll_count=0, now=100)


def test_onstage_plan_bindings_read_active_band_and_accepted_history(pending_side_effect_session):
    from forwin.models.planning_v6 import ArcBeatSheetRow, BandPlanRow
    from forwin.world_engine.plan_bindings import onstage_plan_bindings

    session, conflict = pending_side_effect_session
    project_id = conflict.project_id
    arc = ArcPlanVersion(project_id=project_id, arc_synopsis="arc", chapter_start=1, chapter_end=8, status="active")
    session.add(arc)
    session.flush()
    session.add_all([
        ChapterPlan(project_id=project_id, arc_plan_id=arc.id, chapter_number=1, title="one", status="accepted",
                    world_bindings_json=json.dumps([{"kind": "intent", "ref": "earlier"}])),
        ChapterPlan(project_id=project_id, arc_plan_id=arc.id, chapter_number=4, title="four", status="accepted",
                    world_bindings_json=json.dumps([{"kind": "intent", "ref": "current"},
                                                   {"kind": "entity", "ref": "not-an-intent"}])),
    ])
    sheet = ArcBeatSheetRow(project_id=project_id, arc_plan_id=arc.id, protagonist_ref="hero",
                           band_breakdown_json=json.dumps([{"band_index": 0, "chapter_start": 1, "chapter_end": 8}]))
    session.add(sheet)
    session.flush()
    band = BandPlanRow(project_id=project_id, arc_plan_id=arc.id, beat_sheet_id=sheet.id,
                       band_index=0, chapter_start=1, chapter_end=4, status="active")
    session.add(band)
    session.flush()
    assert onstage_plan_bindings(session, project_id, 4) == (
        frozenset({"current"}), True, frozenset({"earlier", "current"}),
    )
    band.status = "superseded"
    assert onstage_plan_bindings(session, project_id, 4)[1] is False
    assert onstage_plan_bindings(session, project_id, 8) == (
        frozenset(), True, frozenset({"earlier", "current"}),
    )
    sheet.status = "superseded"
    assert onstage_plan_bindings(session, project_id, 8) == (frozenset(), False, frozenset())


# Fix round 1: failed world-tick amendments, transaction blocks, re-splits, partial failures, refs, retries.


def _world_conflict(Session, world) -> str:
    held = TickCandidate(
        candidate_id="adjudication:held", origin="adjudication", actor_id=world.hero, summary="summary-held",
        effects=WorldEffects(entity_changes=[EffectEntityChange(ref=world.rival, field="mood", new_value="wary")]),
        impact_level="major",
    )
    with Session.begin() as session:
        return WorldConflictRepository(session).create(ConflictDraft(
            project_id=world.project_id, source="world_tick", detected_after_chapter=0, story_time=1000,
            involved_refs=[world.rival], violated="impact_review", impact_level="major", summary=SUMMARY,
            proposed_change=held.model_dump(mode="json"), actor_ref=world.hero, blocks_from_chapter=1,
        ), required_decider="human", options=("veto_world", "plot_responds", "amend_plan")).id


def _amend_failed_events(Session, conflict_id: str) -> list[dict[str, Any]]:
    """Each WORLD_CONFLICT_AMEND_FAILED event's payload, with its row id as ``event_id``."""
    with Session() as session:
        rows = session.scalars(select(DecisionEvent).where(
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_AMEND_FAILED,
            DecisionEvent.related_object_id == conflict_id,
        ))
        return [{**json.loads(row.payload_json), "event_id": row.id} for row in rows]


def _active_spans(Session, project_id: str) -> list[tuple[int, int]]:
    from forwin.models.planning_v6 import BandPlanRow

    with Session() as session:
        return sorted((row.chapter_start, row.chapter_end) for row in session.scalars(select(BandPlanRow).where(
            BandPlanRow.project_id == project_id, BandPlanRow.status == "active",
        )))


def test_world_amend_replan_failure_is_recorded_with_a_follow_up_conflict() -> None:
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import _band

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _band_world(Session)
    conflict_id = _world_conflict(Session, world)
    down = StubLLM(RuntimeError("provider down"), RuntimeError("provider down"))

    result = _amend(Session, world.project_id, conflict_id, _amender(down, StubLLM()))

    # The resolution stands; the failed amendment is a warning, an event, result refs and a plan conflict.
    assert len(result.graph_delta_ids) == 1 and result.band_plan_id == ""
    assert [warning["code"] for warning in result.warnings] == ["replan_failed"]
    assert "llm_unavailable" in result.warnings[0]["message"]
    (event,) = _amend_failed_events(Session, conflict_id)
    assert [issue["code"] for issue in event["issues"]] == ["llm_unavailable"]
    assert event["followup_conflict_id"] == result.followup_conflict_id
    status, refs = _conflict_row(Session, world.project_id, conflict_id)
    assert status == "resolved"
    assert refs == [*result.graph_delta_ids, f"amend_failed:{event['event_id']}",
                    f"plan_conflict:{result.followup_conflict_id}"]
    with Session() as session:
        followup = session.get(WorldPlanConflictRow, result.followup_conflict_id)
        assert (followup.source, followup.status, followup.required_decider, followup.blocks_from_chapter) == (
            "plan_feasibility", "open", "human", 1,
        )
        assert json.loads(followup.options_json) == ["amend_plan"]
        proposed = json.loads(followup.proposed_change_json)
        assert (proposed["band"], proposed["band_plan_id"]) == ([1, 4], band_plan_id)

    # The follow-up is the human's way to plan the band again.
    band_client = StubLLM(_band(world, range(1, 5), _FEASIBLE))
    retried = _amend(Session, world.project_id, result.followup_conflict_id, _amender(band_client, StubLLM()))
    assert band_client.payload(0)["notes"] == ["rationale-1"]
    assert _conflict_row(Session, world.project_id, result.followup_conflict_id) == (
        "resolved", [f"band_plan:{retried.band_plan_id}"],
    )
    engine.dispose()


def test_world_amend_that_raises_is_recorded_without_failing_the_resolution() -> None:
    from forwin.planning.amend import PlanAmender
    from tests.test_arc_beat_sheet import StubLLM

    class Raising(PlanAmender):
        def amend(self, session, **kwargs):
            raise LookupError("band plan vanished")

    engine, Session = _session_factory()
    world, _arc_id, _band_plan_id = _band_world(Session)
    conflict_id = _world_conflict(Session, world)
    amender = _amender(StubLLM(), StubLLM())

    result = _amend(Session, world.project_id, conflict_id,
                    Raising(amender.band_planner, amender.beat_sheet_service))

    assert "LookupError: band plan vanished" in result.warnings[0]["message"]
    (event,) = _amend_failed_events(Session, conflict_id)
    assert [issue["code"] for issue in event["issues"]] == ["amend_error"]
    assert _conflict_row(Session, world.project_id, conflict_id)[0] == "resolved"
    engine.dispose()


def test_world_amend_with_no_planned_band_records_no_failure() -> None:
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import _book as band_book

    engine, Session = _session_factory()
    world, _arc_id, _sheet_id = band_book(Session)
    conflict_id = _world_conflict(Session, world)
    band_client = StubLLM()

    result = _amend(Session, world.project_id, conflict_id, _amender(band_client, StubLLM()))

    # The next chapter's band will be planned from the changed world when it comes up.
    assert (band_client.calls, result.warnings, result.band_plan_id, result.followup_conflict_id) == ([], (), "", "")
    assert _amend_failed_events(Session, conflict_id) == []
    assert _conflict_row(Session, world.project_id, conflict_id) == ("resolved", list(result.graph_delta_ids))
    engine.dispose()


@pytest.mark.parametrize("nested", [False, True], ids=["begin_block", "savepoint"])
def test_amend_plan_refuses_a_transaction_block(nested: bool) -> None:
    from tests.test_arc_beat_sheet import StubLLM

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _band_world(Session)
    conflict_id = _plan_conflict(Session, world, band_plan_id=band_plan_id)
    band_client = StubLLM()

    with Session.begin() as session:
        service = ConflictResolutionService(session, admission=CanonAdmissionService(), policy=POLICY,
                                            plan_amender=_amender(band_client, StubLLM()))
        with pytest.raises(RuntimeError, match="outside a transaction block"):
            if nested:
                with session.begin_nested():
                    service.resolve(project_id=world.project_id, conflict_id=conflict_id,
                                    decision=_decision("amend_plan"))
            else:
                service.resolve(project_id=world.project_id, conflict_id=conflict_id, decision=_decision("amend_plan"))

    assert band_client.calls == []
    assert _conflict_row(Session, world.project_id, conflict_id) == ("open", [])
    assert _active_spans(Session, world.project_id) == [(1, 4)]
    engine.dispose()


# test_arc_beat_sheet._sheet's hard milestone, due by chapter 8, carried by chapter 4.
_CARRIES_REVISED_MILESTONE = {4: {"milestone_ids": ["milestone-1"]}}


def _two_band_world(Session):
    """The band world with chapters 5-8 planned too, as band plan version 1 of band 1."""
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import _band, _plan_band, _planner
    from forwin.planning.feasibility import FeasibilityAuditor

    world, arc_id, band_plan_id = _band_world(Session)
    with Session() as session:
        from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
        sheet_id = ArcBeatSheetRepository(session).active_for_arc(arc_id).id
    _plan_band(Session, _planner(StubLLM(_band(world, range(5, 9))), FeasibilityAuditor()), world, arc_id, sheet_id,
               band=(1, 5, 8))
    return world, arc_id, band_plan_id


def test_resplit_amendment_supersedes_stale_band_plans_and_plans_their_rows() -> None:
    from tests.test_arc_beat_sheet import StubLLM, _sheet
    from tests.test_band_planner import _band

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _two_band_world(Session)
    assert _active_spans(Session, world.project_id) == [(1, 4), (5, 8)]
    conflict_id = _plan_conflict(Session, world, violated="milestone", band_plan_id=band_plan_id)
    sheet_client = StubLLM(_sheet(world, bands=((1, 6), (7, 12))))
    # The revised sheet's hard milestone is due by chapter 8, and rows 7-8 still count as planned.
    band_client = StubLLM(_band(world, range(1, 7), _CARRIES_REVISED_MILESTONE), _band(world, range(7, 13)))

    result = _amend(Session, world.project_id, conflict_id, _amender(band_client, sheet_client))

    # Chapters 5-8 were band 5-8's: they now belong to bands 1-6 and 7-12, and no stale band plan overlaps.
    assert [band_client.payload(call)["band"]["chapter_start"] for call in (0, 1)] == [1, 7]
    assert _active_spans(Session, world.project_id) == [(1, 6), (7, 12)]
    assert _conflict_row(Session, world.project_id, conflict_id)[0] == "resolved"
    assert result.band_plan_id
    engine.dispose()


@pytest.mark.parametrize("failing_band", [1, 2], ids=["first_band_fails", "second_band_fails"])
def test_amendment_succeeds_only_when_every_band_is_planned(failing_band: int) -> None:
    from tests.test_arc_beat_sheet import StubLLM, _sheet
    from tests.test_band_planner import _band

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _two_band_world(Session)
    conflict_id = _plan_conflict(Session, world, violated="milestone", band_plan_id=band_plan_id)
    down = [RuntimeError("provider down"), RuntimeError("provider down")]
    answers = [*down] if failing_band == 1 else [_band(world, range(1, 7), _CARRIES_REVISED_MILESTONE), *down]
    band_client = StubLLM(*answers)

    with pytest.raises(ConflictOptionUnavailable, match="llm_unavailable"):
        _amend(Session, world.project_id, conflict_id,
               _amender(band_client, StubLLM(_sheet(world, bands=((1, 6), (7, 12))))))

    # A failing band stops the amendment: no model call plans a later band.
    assert len(band_client.calls) == len(answers)
    # The revision and the failure are recorded: the amendment made no plan of the whole range.
    status, refs = _conflict_row(Session, world.project_id, conflict_id)
    assert status == "open" and [ref.split(":")[0] for ref in refs] == ["beat_sheet", "amend_failed"]
    engine.dispose()


def test_retry_after_a_failed_revision_amendment_keeps_the_revised_sheet() -> None:
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from tests.test_arc_beat_sheet import StubLLM, _sheet
    from tests.test_band_planner import _band

    engine, Session = _session_factory()
    world, arc_id, band_plan_id = _band_world(Session)
    conflict_id = _plan_conflict(Session, world, violated="milestone", band_plan_id=band_plan_id)
    down = StubLLM(RuntimeError("provider down"), RuntimeError("provider down"))
    with pytest.raises(ConflictOptionUnavailable):
        _amend(Session, world.project_id, conflict_id,
               _amender(down, StubLLM(_sheet(world, bands=((1, 4), (5, 8), (9, 12))))))

    # The retry plans from the revision the first attempt made: no second revision.
    sheet_client = StubLLM()
    result = _amend(Session, world.project_id, conflict_id,
                    _amender(StubLLM(_band(world, range(1, 5))), sheet_client))

    assert sheet_client.calls == []
    with Session() as session:
        sheet = ArcBeatSheetRepository(session).active_for_arc(arc_id)
        assert (sheet.version, sheet.id) == (2, result.beat_sheet_id)
    status, refs = _conflict_row(Session, world.project_id, conflict_id)
    assert status == "resolved"
    # The first attempt's failure stays on record before the revision and the plan that resolved it.
    assert [ref for ref in refs if not ref.startswith("amend_failed:")] == [
        f"beat_sheet:{result.beat_sheet_id}", f"band_plan:{result.band_plan_id}",
    ]
    assert sum(ref.startswith("amend_failed:") for ref in refs) == 1
    engine.dispose()


def test_amendment_result_refs_survive_a_concurrent_ref() -> None:
    from tests.test_band_planner import HookedLLM, _band

    engine, Session = _session_factory()
    world, _arc_id, band_plan_id = _band_world(Session)
    conflict_id = _plan_conflict(Session, world, band_plan_id=band_plan_id)

    def concurrent_ref() -> None:
        with Session.begin() as other:
            repo = WorldConflictRepository(other)
            repo.attach_result_refs(repo.get(world.project_id, conflict_id, for_update=True), ["other:ref"])

    from tests.test_arc_beat_sheet import StubLLM
    band_client = HookedLLM(concurrent_ref, _band(world, range(1, 5), _FEASIBLE))
    result = _amend(Session, world.project_id, conflict_id, _amender(band_client, StubLLM()))

    assert _conflict_row(Session, world.project_id, conflict_id) == (
        "resolved", ["other:ref", f"band_plan:{result.band_plan_id}"],
    )
    engine.dispose()


def test_amend_plan_on_a_band_planning_conflict_plans_the_band() -> None:
    from forwin.models.planning_v6 import BandPlanRow
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import _band, _book as band_book

    engine, Session = _session_factory()
    world, _arc_id, _sheet_id = band_book(Session)
    issues = [{"code": "llm_unavailable", "severity": "error", "chapter_number": None, "ref": "", "message": "m"}]
    with Session.begin() as session:
        conflict_id = WorldConflictRepository(session).create(ConflictDraft(
            project_id=world.project_id, source="band_planning", detected_after_chapter=0, story_time=1000,
            involved_refs=[], violated="feasibility", impact_level="major", summary=PLAN_CONFLICT_SUMMARY,
            proposed_change={"kind": "plan_issues", "band": [1, 4], "issues": issues}, blocks_from_chapter=1,
        ), required_decider="human", options=("amend_plan",)).id
    band_client = StubLLM(_band(world, range(1, 5), _FEASIBLE))

    result = _amend(Session, world.project_id, conflict_id, _amender(band_client, StubLLM()))

    assert band_client.payload(0)["chapters_to_plan"] == [1, 2, 3, 4]
    with Session() as session:
        planned = session.get(BandPlanRow, result.band_plan_id)
        assert (planned.version, planned.chapter_start, planned.chapter_end) == (1, 1, 4)
    assert _conflict_row(Session, world.project_id, conflict_id) == ("resolved", [f"band_plan:{result.band_plan_id}"])
    engine.dispose()


# Task 8 fix round 1: planless side effects and rollback contracts.

@pytest.mark.parametrize("option", ["plot_responds", "accept_with_obligation"])
def test_supersede_planless_resolution_retires_its_side_effect(pending_side_effect_session, option):
    from forwin.world_engine.conflict_resolution import record_option_side_effects
    from forwin.world_engine.superseded_resolutions import supersede_resolutions

    session, row = pending_side_effect_session
    decision = _decision(option)
    effects = record_option_side_effects(session, row, decision, latest_chapter=1)
    repo = WorldConflictRepository(session)
    refs = ["rolled-back-delta"] + ([effects.obligation_id] if effects.obligation_id else [])
    repo.mark_resolved(row, decision=decision, result_refs=refs,
                       pending_plan_response=effects.pending_plan_response)
    result = supersede_resolutions(session, project_id=row.project_id, graph_delta_id="rolled-back-delta",
                                  reason="historical rewrite")
    assert result.conflict_ids == (row.id,)
    assert row.status == "resolved"
    assert repo.pending_plot_responses(row.project_id) == []
    if effects.obligation_id:
        obligation = session.get(NarrativeObligationRow, effects.obligation_id)
        assert obligation.status == "cancelled"
        assert json.loads(obligation.metadata_json)["cancel_reason"] == "historical rewrite"
        assert result.cancelled_obligation_ids == (effects.obligation_id,)
    else:
        assert json.loads(row.decision_json)["pending_plan_response"] is False
        assert result.cancelled_obligation_ids == ()


def test_supersede_planner_bound_response_reverts_prefixed_patch(pending_side_effect_session):
    from forwin.world_engine.conflict_resolution import record_option_side_effects, record_plot_response_patch
    from forwin.world_engine.superseded_resolutions import supersede_resolutions

    session, row = pending_side_effect_session
    repo = WorldConflictRepository(session)
    decision = _decision("plot_responds")
    effects = record_option_side_effects(session, row, decision, latest_chapter=1)
    repo.mark_resolved(row, decision=decision, result_refs=["delta"],
                       pending_plan_response=effects.pending_plan_response)
    arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=8)
    session.add(arc)
    session.flush()
    plan = ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=2, title="plan",
                       goals_json='["original"]', status="planned")
    session.add(plan)
    session.flush()
    patch_id = record_plot_response_patch(session, row, plan)
    repo.attach_result_refs(row, [f"plan_patch:{patch_id}"])
    assert len(load_goals_json(plan.goals_json)) == 2
    result = supersede_resolutions(session, project_id=row.project_id, graph_delta_id="delta", reason="rewrite")
    assert result.reverted_plan_patch_ids == (patch_id,)
    assert load_goals_json(plan.goals_json) == ["original"]
    assert row.status == "resolved"
    assert repo.pending_plot_responses(row.project_id) == []


@pytest.mark.parametrize("has_plan", [False, True], ids=["planless", "with-plan"])
def test_obligation_refuses_full_ledger_without_creating_row(pending_side_effect_session, has_plan):
    from forwin.world_engine.conflict_resolution import record_option_side_effects

    session, row = pending_side_effect_session
    if has_plan:
        arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=8)
        session.add(arc)
        session.flush()
        session.add(ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=2,
                                title="next", status="planned"))
    for index in range(3):
        session.add(NarrativeObligationRow(
            id=f"existing-{index}", project_id=row.project_id, origin_chapter_number=1,
            obligation_type="motivation_gap", priority="P1", status="active", summary="existing",
            hardness="design_debt", deadline_chapter=5, payoff_test="payoff",
        ))
    session.flush()
    with pytest.raises(ConflictOptionUnavailable, match="obligation_budget_exceeded"):
        record_option_side_effects(session, row, _decision("accept_with_obligation"), latest_chapter=1)
    assert row.status == "open"
    assert len(session.scalars(select(NarrativeObligationRow)).all()) == 3


@pytest.mark.parametrize("rationale", ["plain", 'quote \" slash \\ newline\n tab\t', "理由/<>"])
def test_repository_veto_serializer_matches_recent_vetoes_sql_like(pending_side_effect_session, rationale):
    from forwin.world_engine.actor_turns import recent_vetoes

    session, row = pending_side_effect_session
    row.actor_ref = "actor"
    repo = WorldConflictRepository(session)
    repo.mark_resolved(row, decision=_decision("veto_world").model_copy(update={"rationale": rationale}),
                       result_refs=[], pending_plan_response=True)
    assert recent_vetoes(session, row.project_id, actor_ids=["actor"])["actor"] == [row]
    repo.clear_pending_plan_response(row)
    assert recent_vetoes(session, row.project_id, actor_ids=["actor"])["actor"] == [row]
    assert json.loads(row.decision_json)["rationale"] == rationale


def test_planless_obligation_is_in_band_planner_inputs(pending_side_effect_session):
    from forwin.world_engine.conflict_resolution import record_option_side_effects
    from forwin.planning.band_planner.inputs import build_band_inputs
    from forwin.planning.beat_sheet.types import ArcBeatSheetData, BandSpan
    from tests.test_world_tick_steps import _actor, _runtime as memory_runtime

    session, row = pending_side_effect_session
    effects = record_option_side_effects(session, row, _decision("accept_with_obligation"), latest_chapter=1)
    arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=8)
    session.add(arc)
    session.flush()
    band = BandSpan(band_index=0, chapter_start=2, chapter_end=4)
    inputs = build_band_inputs(
        session, project_id=row.project_id, arc_plan=arc,
        beat_sheet=ArcBeatSheetData(protagonist_ref="actor", phases=[], band_breakdown=[band]),
        band=band, chapter_numbers=[2, 3, 4], rows={}, as_of=1, runtime=memory_runtime(_actor("actor")),
        policy=POLICY, notes=[], trope_cost_ceiling=10,
    )
    assert [item["id"] for item in inputs.payload["open_obligations"]] == [effects.obligation_id]


@pytest.mark.parametrize("option", ["plot_responds", "accept_with_obligation"])
def test_resolution_without_plan_reaches_later_band_planner(option):
    from tests.test_band_planner import (
        _book as planner_book, _band, _planner, _plan_band, StubLLM, _intent as planner_intent,
    )

    engine, Session = _session_factory()
    world, arc_id, sheet_id = planner_book(Session)
    held = TickCandidate(candidate_id="held", origin="adjudication", actor_id=world.hero,
                         intent_id=planner_intent(world), summary="held",
                         effects=WorldEffects(entity_changes=[
                             EffectEntityChange(ref=world.rival, field="mood", new_value="wary"),
                         ]))
    with Session.begin() as session:
        session.get(Project, world.project_id).target_total_chapters = 20
        row = WorldConflictRepository(session).create(ConflictDraft(
            project_id=world.project_id, source="world_tick", detected_after_chapter=0, story_time=1000,
            involved_refs=[world.rival], violated="impact_review", impact_level="moderate", summary="held",
            proposed_change=held.model_dump(mode="json"), actor_ref=world.hero,
            intent_ref=planner_intent(world), blocks_from_chapter=1,
        ), required_decider="human", options=ALL_OPTIONS)
        conflict_id = row.id
    with Session.begin() as session:
        result = _service(session).resolve(project_id=world.project_id, conflict_id=conflict_id,
                                           decision=_decision(option))
    client = StubLLM(_band(world, range(1, 5), {1: {"goals": [PLOT_RESPONSE_GENERIC_GOAL, "goal-1"]}}))
    outcome = _plan_band(Session, _planner(client), world, arc_id, sheet_id)
    assert outcome.band_plan_id is not None
    with Session() as session:
        repo = WorldConflictRepository(session)
        row = repo.get(world.project_id, conflict_id)
        if option == "plot_responds":
            assert client.payload(0)["pending_responses"][0]["id"] == conflict_id
            patch_refs = [ref for ref in json.loads(row.result_refs_json) if ref.startswith("plan_patch:")]
            assert len(patch_refs) == 1
            assert session.get(NarrativePlanPatchRow, patch_refs[0].removeprefix("plan_patch:")) is not None
            assert repo.pending_plot_responses(world.project_id) == []
        else:
            assert [item["id"] for item in client.payload(0)["open_obligations"]] == [result.obligation_id]
    engine.dispose()


# R42: the plot answers a live intent before its outcome can land.
def test_plot_responds_holds_the_intent_and_commits_no_world_change(pending_side_effect_session):
    from tests.test_world_tick_steps import _actor, _intent, _runtime as runtime_for
    from forwin.book_state.lexicon import BookLexicon
    from forwin.world_engine.delta_builder import _TickBuilder
    from forwin.world_engine.types import TickContext

    _, row = pending_side_effect_session
    row.intent_ref = "intent"
    row.actor_ref = "actor"
    held = TickCandidate(candidate_id="held", origin="adjudication", summary="private",
                         intent_id="intent", effects=WorldEffects(entity_changes=[
                             EffectEntityChange(ref="actor", field="alive_status", new_value="dead"),
                         ]), intent_status_changes={"intent": "resolved"})
    row.proposed_change_json = held.model_dump_json()
    runtime = runtime_for(_actor("actor"), _intent("intent", "actor", at=5))
    candidates = option_candidates(row, _decision("plot_responds"), runtime=runtime,
                                   policy=POLICY, reroll_count=0, now=100)
    built = _TickBuilder(context=TickContext(project_id=runtime.project_id, chapter_number=3,
                                            now=100, window_start=0, seed=1), tick_id="hold",
                         runtime=runtime, calendar=None, lexicon=BookLexicon()).build(candidates, [])
    assert built.delta is not None, built.issues
    assert _patches(built.delta) == [("intent", "set", "state.status", "planned", "held")]
    assert not built.delta.fact_patches and not built.delta.cognition_patches


@pytest.mark.parametrize("status", ["missing", "resolved", "disrupted", "vetoed", "inactive", "actor"])
def test_plot_responds_unavailable_without_a_live_intent(pending_side_effect_session, status):
    from tests.test_world_tick_steps import _actor, _intent, _runtime as runtime_for
    from forwin.world_engine.conflict_routing import available_options, live_intent

    _, row = pending_side_effect_session
    row.intent_ref = "intent"
    row.proposed_change_json = TickCandidate(candidate_id="held", origin="adjudication", summary="held",
                                            effects=WorldEffects()).model_dump_json()
    node = (_actor("intent") if status == "actor" else
            _intent("intent", "actor", at=5, status="planned" if status == "inactive" else status))
    if status == "inactive":
        node = node.model_copy(update={"is_active": False})
    runtime = runtime_for(*([] if status == "missing" else [node]))
    draft = ConflictDraft(project_id=row.project_id, source="world_tick", detected_after_chapter=1,
                          story_time=100, involved_refs=[], violated="impact_review", impact_level="moderate",
                          summary="held", proposed_change={}, intent_ref="intent", blocks_from_chapter=2)
    assert "plot_responds" not in available_options(draft, live_intent=live_intent(runtime, row.intent_ref))
    with pytest.raises(ConflictOptionUnavailable):
        option_candidates(row, _decision("plot_responds"), runtime=runtime, policy=POLICY, reroll_count=0, now=100)


def test_plot_responds_is_offered_once_per_intent(pending_side_effect_session):
    from forwin.world_engine.conflict_routing import available_options
    from tests.test_world_tick_steps import _intent, _runtime as runtime_for

    session, row = pending_side_effect_session
    row.intent_ref = "intent"
    repo = WorldConflictRepository(session)
    draft = ConflictDraft(project_id=row.project_id, source="world_tick", detected_after_chapter=2,
                          story_time=100, involved_refs=[], violated="impact_review", impact_level="moderate",
                          summary="held", proposed_change={}, intent_ref="intent", blocks_from_chapter=3)
    assert "plot_responds" in available_options(draft, live_intent=True,
                                               plot_responded=repo.has_plot_response(row.project_id, "intent"))
    repo.mark_resolved(row, decision=_decision("plot_responds"), result_refs=["hold"], pending_plan_response=True)
    assert "plot_responds" not in available_options(draft, live_intent=True,
                                                   plot_responded=repo.has_plot_response(row.project_id, "intent"))
    assert not repo.has_plot_response(row.project_id, "other")
    from forwin.world_engine.tick_conflicts import TickConflicts
    from forwin.world_engine.types import TickContext
    routed = TickConflicts(session, context=TickContext(project_id=row.project_id, chapter_number=2,
                                                       now=100, window_start=0, seed=1),
                           runtime=runtime_for(_intent("intent", "a", at=1)),
                           policy=_policy(conflict_delegate="human"), tick_id="again", delegate=None,
                           audit_writer_factory=lambda: None)
    routed.hold(draft, extra={})
    offered = repo.get(row.project_id, routed.conflict_ids[0])
    assert "plot_responds" not in json.loads(offered.options_json)
    stale_offer = repo.create(draft, required_decider="human", options=ALL_OPTIONS)
    with pytest.raises(ConflictOptionUnavailable):
        option_candidates(stale_offer, _decision("plot_responds"), runtime=runtime_for(_intent("intent", "a", at=1)),
                          policy=POLICY, reroll_count=0, now=100, plot_responded=True)


def test_plot_response_binding_survives_replanning_and_reverts(pending_side_effect_session):
    from forwin.world_engine.conflict_resolution import record_plot_response_patch
    from forwin.narrative_obligations.repository import NarrativeObligationRepository
    from forwin.planning.band_planner.persistence import reapply_active_patches
    from forwin.world_engine.superseded_resolutions import supersede_resolutions

    session, row = pending_side_effect_session
    row.intent_ref = "held-intent"
    arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=8)
    session.add(arc)
    session.flush()
    plan = ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=2, title="response",
                       goals_json='["original"]', world_bindings_json='[]', status="planned")
    session.add(plan)
    session.flush()
    patch_id = record_plot_response_patch(session, row, plan)
    repo = WorldConflictRepository(session)
    repo.mark_resolved(row, decision=_decision("plot_responds"), result_refs=["hold", f"plan_patch:{patch_id}"],
                       pending_plan_response=True)
    binding = {"kind": "intent", "ref": "held-intent", "how": PLOT_RESPONSE_GENERIC_GOAL}
    assert json.loads(plan.world_bindings_json) == [binding]
    plan.world_bindings_json = '[]'
    reapply_active_patches(session, plan)
    reapply_active_patches(session, plan)
    assert json.loads(plan.world_bindings_json) == [binding]
    supersede_resolutions(session, project_id=row.project_id, graph_delta_id="hold", reason="rewrite")
    assert json.loads(plan.world_bindings_json) == []
    assert json.loads(row.decision_json)["pending_plan_response"] is False
    assert repo.has_plot_response(row.project_id, row.intent_ref)
    assert len(NarrativeObligationRepository(session).list_patches_for_plan(row.project_id, target_plan_id=plan.id)) == 2


@pytest.mark.parametrize("impact,expected", [
    ("moderate", ("veto_world", "amend_plan", "accept_with_obligation")),
    ("major", ("veto_world", "amend_plan")),
])
def test_answered_intent_keeps_existing_acceptance_eligibility(impact, expected):
    from forwin.world_engine.conflict_routing import available_options

    draft = ConflictDraft(project_id="project", source="world_tick", detected_after_chapter=2,
                          story_time=100, involved_refs=[], violated="impact_review", impact_level=impact,
                          summary="held", proposed_change={}, intent_ref="intent", blocks_from_chapter=3)
    assert available_options(draft, live_intent=True, plot_responded=True) == expected


def test_accept_with_obligation_reports_builder_warnings(monkeypatch):
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    held = _held(book)
    held = held.model_copy(update={"effects": held.effects.model_copy(update={"relation_changes": [
        EffectRelationChange(source_ref=book.world.hero, target_ref=book.world.rival,
                             edge_type="ally_of", change="end"),
    ]})})
    conflict_id = _conflict(Session, book, proposed_change=held.model_dump(mode="json"))
    result = _resolve(Session, book, conflict_id, "accept_with_obligation")
    assert [warning["code"] for warning in result.warnings] == ["relation_missing"]
    [(_, payload)] = _resolved_events(Session, book, conflict_id)
    assert payload["warnings"] == [dict(warning) for warning in result.warnings]
    assert set(payload["warnings"][0]) == {"code", "message"}
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "resolved"
    engine.dispose()


@pytest.mark.parametrize("last_chapter", [False, True], ids=["continuing", "final"])
@pytest.mark.parametrize("effect,expected", [(None, "planned"), ("advanced", "planned"),
                                             ("fulfilled", "resolved"), ("disrupted", "disrupted")])
def test_bound_hold_lifecycle_through_tick_without_postgres(pending_side_effect_session, last_chapter, effect, expected):
    from types import SimpleNamespace
    from forwin.models.canon import CanonCommitRecord
    from forwin.protocol.book_state import WorldNode
    from forwin.world_engine.tick import WorldTickService

    session, row = pending_side_effect_session
    project_id = row.project_id
    session.get(Project, project_id).target_total_chapters = 1 if last_chapter else 20
    repo = BookStateRepository(session)
    for node in (
        WorldNode(id="actor", project_id=project_id, node_type="character", name="actor",
                  state={"alive_status": "alive", "next_turn_at": 200}),
        WorldNode(id="intent", project_id=project_id, node_type="intent", name="intent",
                  profile={"owner_ref": "actor"}, state={"status": "held", "scheduled_at": 5}),
    ):
        repo.create_world_node(node, version_chapter=1, version_state=node.state)
    arc = ArcPlanVersion(project_id=project_id, arc_synopsis="arc", chapter_start=1, chapter_end=20)
    session.add(arc)
    session.flush()
    session.add(ChapterPlan(project_id=project_id, arc_plan_id=arc.id, chapter_number=1, title="answer",
                            status="accepted", world_bindings_json='[{"kind":"intent","ref":"intent"}]'))
    chapter = GraphDelta(id="chapter", project_id=project_id, chapter_number=1, summary="chapter",
                         source_type="chapter_extraction", story_time_start=0, story_time_end=100,
                         metadata={"intent_interactions": ([{"intent_id": "intent", "effect": effect}]
                                                           if effect else [])})
    repo.append_graph_delta(chapter)
    session.flush()
    committed = []

    class CaptureAdmission:
        def commit_world_tick(self, **kwargs):
            committed.extend(kwargs["approved_changes"].graph_deltas)
            return SimpleNamespace(idempotent=False, graph_delta_ids=tuple(delta.id for delta in committed))

    service = WorldTickService(llm_client=None, admission=CaptureAdmission(), adjudicator=None, actor_runner=None,
                               conflict_delegate=None, audit_writer_factory=lambda: None, save_prompt_trace=lambda *_: "")
    result = service.run(session, CanonCommitRecord(id="commit", project_id=project_id, chapter_number=1,
                                                    graph_delta_ids_json='["chapter"]'), policy=POLICY)
    assert result.graph_delta_ids and not result.skipped_reason
    [delta] = committed
    expected_patches = [("intent", "set", "state.status", "held", expected)]
    if expected == "planned":
        expected_patches.append(("intent", "set", "state.scheduled_at", 5, 100))
    assert _patches(delta) == expected_patches
    assert not delta.fact_patches and not delta.cognition_patches


def test_plot_response_preserves_due_news_without_committing_it(pending_side_effect_session):
    from types import SimpleNamespace
    from forwin.protocol.book_state import FactNode, WorldNode

    session, row = pending_side_effect_session
    project_id = row.project_id
    repo = BookStateRepository(session)
    for node in (
        WorldNode(id="actor", project_id=project_id, node_type="character", state={"alive_status": "alive"}),
        WorldNode(id="intent", project_id=project_id, node_type="intent", state={"status": "planned"}),
    ):
        repo.create_world_node(node, version_chapter=1, version_state=node.state)
    repo.create_fact_node(FactNode(id="news", project_id=project_id, proposition="news"))
    queue = [{"fact_ref": "fact:news", "observer_ref": "actor", "arrive_at": 50}]
    repo.append_graph_delta(GraphDelta(id="earlier", project_id=project_id, chapter_number=1, summary="earlier",
                                       source_type="world_engine", story_time_start=0, story_time_end=100,
                                       metadata={"news_in_transit": queue}))
    row.intent_ref = "intent"
    row.proposed_change_json = TickCandidate(candidate_id="held", origin="adjudication", summary="held",
                                            intent_id="intent", effects=WorldEffects()).model_dump_json()
    session.flush()
    committed = []

    class CaptureAdmission:
        def commit_world_tick(self, **kwargs):
            committed.extend(kwargs["approved_changes"].graph_deltas)
            return SimpleNamespace(graph_delta_ids=tuple(delta.id for delta in committed))

    result = _service(session, admission=CaptureAdmission()).resolve(
        project_id=project_id, conflict_id=row.id, decision=_decision("plot_responds"),
    )
    [delta] = committed
    assert result.graph_delta_ids == (delta.id,)
    assert _patches(delta) == [("intent", "set", "state.status", "planned", "held")]
    assert not delta.cognition_patches
    assert delta.metadata["news_in_transit"] == queue
    assert json.loads(row.decision_json)["pending_plan_response"] is True


# F4 fix round 2: carrier reachability and the book-complete cleanup boundary.
def _response_lifecycle_tick(session, project_id, *, interactions=(), final=False):
    from types import SimpleNamespace
    from forwin.models.canon import CanonCommitRecord
    from forwin.world_engine.tick import WorldTickService

    session.get(Project, project_id).target_total_chapters = 2 if final else 4
    chapter = GraphDelta(id="lifecycle-chapter", project_id=project_id, chapter_number=2,
                         summary="chapter", source_type="chapter_extraction", story_time_start=50,
                         story_time_end=100, metadata={"intent_interactions": list(interactions)})
    BookStateRepository(session).append_graph_delta(chapter)
    session.flush()
    committed = []

    class CaptureAdmission:
        def commit_world_tick(self, **kwargs):
            committed.extend(kwargs["approved_changes"].graph_deltas)
            return SimpleNamespace(idempotent=False, graph_delta_ids=tuple(d.id for d in committed))

    service = WorldTickService(llm_client=None, admission=CaptureAdmission(), adjudicator=None,
                               actor_runner=None, conflict_delegate=None, audit_writer_factory=lambda: None,
                               save_prompt_trace=lambda *_: "")
    result = service.run(session, CanonCommitRecord(id="lifecycle-commit", project_id=project_id,
                         chapter_number=2, graph_delta_ids_json='["lifecycle-chapter"]'), policy=POLICY)
    return result, committed


def _response_lifecycle_nodes(session, project_id, statuses):
    from forwin.protocol.book_state import WorldNode

    repo = BookStateRepository(session)
    for ref, status in statuses.items():
        repo.create_world_node(WorldNode(id=ref, project_id=project_id, node_type="intent", name=ref,
            profile={"owner_ref": "actor"}, state={"status": status, "scheduled_at": 5}),
            version_chapter=1, version_state={"status": status, "scheduled_at": 5})


@pytest.mark.parametrize("carrier_status,carrier_number", [("drafted", 2), ("planned", 3), ("draft-history", 2)])
def test_plot_response_selects_only_unwritten_carriers_within_target(
    pending_side_effect_session, carrier_status, carrier_number,
):
    session, row = pending_side_effect_session
    session.get(Project, row.project_id).target_total_chapters = 2
    arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=4)
    session.add(arc)
    session.flush()
    plan = ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=carrier_number,
                       title="unreachable", status="planned" if carrier_status == "draft-history" else carrier_status)
    session.add(plan)
    session.flush()
    if carrier_status == "draft-history":
        from forwin.models.draft import ChapterDraft
        session.add(ChapterDraft(chapter_plan_id=plan.id, body_text="already written"))
        session.flush()
    from forwin.world_engine.conflict_resolution import record_option_side_effects
    effects = record_option_side_effects(session, row, _decision("plot_responds"), latest_chapter=1)
    assert effects.pending_plan_response and effects.plan_patch_id == ""
    assert json.loads(plan.world_bindings_json or "[]") == []


@pytest.mark.parametrize("carrier", ["unbound", "beyond-target", "missing", "lost-binding"])
def test_final_tick_retires_every_response_without_world_activity(pending_side_effect_session, monkeypatch, carrier):
    from forwin.world_engine import tick
    from forwin.world_engine.conflict_resolution import record_plot_response_patch
    from forwin.planning.band_planner.persistence import reapply_active_patches

    session, row = pending_side_effect_session
    row.intent_ref = "held"
    _response_lifecycle_nodes(session, row.project_id, {"held": "held", "other": "planned", "settled": "resolved"})
    from forwin.protocol.book_state import FactNode, WorldNode
    world = BookStateRepository(session)
    for node in (
        WorldNode(id="actor", project_id=row.project_id, node_type="character",
                  state={"alive_status": "alive", "next_turn_at": 60}),
        WorldNode(id="clock", project_id=row.project_id, node_type="clock",
                  state={"status": "running", "due_at": 60}),
    ):
        world.create_world_node(node, version_chapter=1, version_state=node.state)
    world.create_fact_node(FactNode(id="news", project_id=row.project_id, proposition="queued news"))
    queue = [{"fact_ref": "fact:news", "observer_ref": "actor", "arrive_at": 60}]
    world.append_graph_delta(GraphDelta(id="earlier-news", project_id=row.project_id, chapter_number=1,
        summary="earlier", source_type="world_engine", story_time_start=0, story_time_end=50,
        metadata={"news_in_transit": queue}))
    arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=4)
    session.add(arc)
    session.flush()
    plan = ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=3,
                       title="response", status="planned", goals_json='["original"]')
    session.add(plan)
    session.flush()
    refs = ["hold-delta"]
    if carrier != "unbound":
        refs.append(record_plot_response_patch(session, row, plan))
    repo = WorldConflictRepository(session)
    repo.mark_resolved(row, decision=_decision("plot_responds"), result_refs=refs,
                       pending_plan_response=carrier == "unbound")
    if carrier == "missing":
        session.delete(plan)
        session.flush()
    elif carrier == "lost-binding":
        plan.world_bindings_json = "[]"

    def forbidden(*args, **kwargs):
        pytest.fail("Final cleanup must not run world activity")

    for name in ("expire_clocks", "adjudicate", "take_turns", "propagation_candidate"):
        monkeypatch.setattr(tick, name, forbidden)
    _, deltas = _response_lifecycle_tick(session, row.project_id, final=True, interactions=[
        {"intent_id": "other", "effect": "fulfilled"}, {"intent_id": "settled", "effect": "disrupted"},
    ])
    assert [_patches(d) for d in deltas] == [[
        ("held", "set", "state.status", "held", "planned"),
        ("held", "set", "state.scheduled_at", 5, 100),
    ]]
    assert all(not d.fact_patches and not d.cognition_patches and not d.narrative_patches for d in deltas)
    assert deltas[0].metadata["news_in_transit"] == queue
    assert repo.pending_plot_responses(row.project_id) == []
    assert json.loads(row.decision_json)["pending_plan_response"] is False
    assert repo.has_plot_response(row.project_id, "held")
    if carrier != "missing":
        reapply_active_patches(session, plan)
        assert json.loads(plan.world_bindings_json or "[]") == []
        assert load_goals_json(plan.goals_json) == ["original"]


@pytest.mark.parametrize("held", [False, True])
def test_final_tick_interactions_touch_only_held_intents(pending_side_effect_session, monkeypatch, held):
    from forwin.world_engine import tick

    session, row = pending_side_effect_session
    _response_lifecycle_nodes(session, row.project_id, {"held": "held" if held else "resolved", "other": "planned"})
    def forbidden(*args, **kwargs):
        pytest.fail("Final cleanup must not run world activity")
    for name in ("expire_clocks", "adjudicate", "take_turns", "propagation_candidate"):
        monkeypatch.setattr(tick, name, forbidden)
    result, deltas = _response_lifecycle_tick(session, row.project_id, final=True, interactions=[
        {"intent_id": "held", "effect": "disrupted"}, {"intent_id": "other", "effect": "fulfilled"},
    ])
    assert [_patches(d) for d in deltas] == ([[
        ("held", "set", "state.status", "held", "disrupted"),
    ]] if held else [])
    assert result.skipped_reason == ("" if held else "book_complete")


@pytest.mark.parametrize("invalidate", ["missing", "lost-binding", "beyond-target", "cancelled"])
def test_stale_response_carrier_is_rebound_and_survives_replanning(pending_side_effect_session, invalidate):
    from forwin.world_engine.conflict_resolution import record_plot_response_patch
    from forwin.planning.band_planner.persistence import reapply_active_patches

    session, row = pending_side_effect_session
    row.intent_ref = "held"
    _response_lifecycle_nodes(session, row.project_id, {"held": "held"})
    arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=8)
    session.add(arc)
    session.flush()
    old = ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=5 if invalidate == "beyond-target" else 3,
                      title="old", status="planned", goals_json='["original"]')
    new = ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=4, title="new", status="planned")
    session.add_all([old, new])
    session.flush()
    patch = record_plot_response_patch(session, row, old)
    repo = WorldConflictRepository(session)
    repo.mark_resolved(row, decision=_decision("plot_responds"), result_refs=["hold", patch])
    if invalidate == "missing":
        session.delete(old)
    elif invalidate == "lost-binding":
        old.world_bindings_json = "[]"
        old.status = "drafted"
    elif invalidate == "cancelled":
        old.status = "cancelled"
    session.get(Project, row.project_id).target_total_chapters = 4
    session.flush()
    assert repo.pending_plot_responses(row.project_id) == [row]
    _, deltas = _response_lifecycle_tick(session, row.project_id)
    assert deltas == []
    assert repo.pending_plot_responses(row.project_id) == []
    assert json.loads(new.world_bindings_json)[0]["ref"] == "held"
    new.world_bindings_json = "[]"
    reapply_active_patches(session, new)
    assert [item["ref"] for item in json.loads(new.world_bindings_json)] == ["held"]
    if invalidate != "missing":
        old.status = "planned"
        old.world_bindings_json = "[]"
        assert patch not in reapply_active_patches(session, old)
        assert json.loads(old.world_bindings_json) == []


def test_response_recorded_while_final_chapter_is_drafted_is_retired_on_acceptance(pending_side_effect_session):
    from forwin.world_engine.conflict_resolution import record_option_side_effects

    session, row = pending_side_effect_session
    row.intent_ref = "held"
    _response_lifecycle_nodes(session, row.project_id, {"held": "held"})
    session.get(Project, row.project_id).target_total_chapters = 2
    arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=2)
    session.add(arc)
    session.flush()
    final = ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=2,
                        title="final", status="drafted")
    session.add(final)
    session.flush()
    effects = record_option_side_effects(session, row, _decision("plot_responds"), latest_chapter=1)
    assert effects.pending_plan_response and not effects.plan_patch_id
    repo = WorldConflictRepository(session)
    repo.mark_resolved(row, decision=_decision("plot_responds"), result_refs=["hold"], pending_plan_response=True)
    final.status = "accepted"
    _, deltas = _response_lifecycle_tick(session, row.project_id, final=True)
    assert [_patches(d) for d in deltas] == [[
        ("held", "set", "state.status", "held", "planned"),
        ("held", "set", "state.scheduled_at", 5, 100),
    ]]
    assert repo.pending_plot_responses(row.project_id) == []
    assert json.loads(row.decision_json)["response_retired"] is True


def test_stale_response_without_replacement_stays_pending(pending_side_effect_session):
    from forwin.world_engine.conflict_resolution import record_plot_response_patch

    session, row = pending_side_effect_session
    row.intent_ref = "held"
    _response_lifecycle_nodes(session, row.project_id, {"held": "held"})
    arc = ArcPlanVersion(project_id=row.project_id, arc_synopsis="arc", chapter_start=1, chapter_end=8)
    session.add(arc)
    session.flush()
    plan = ChapterPlan(project_id=row.project_id, arc_plan_id=arc.id, chapter_number=5, title="beyond", status="planned")
    session.add(plan)
    session.flush()
    patch_id = record_plot_response_patch(session, row, plan)
    repo = WorldConflictRepository(session)
    repo.mark_resolved(row, decision=_decision("plot_responds"), result_refs=["hold", patch_id])
    _, deltas = _response_lifecycle_tick(session, row.project_id)
    assert deltas == []
    assert repo.pending_plot_responses(row.project_id) == [row]
    assert json.loads(row.decision_json)["pending_plan_response"] is True
    assert repo.response_patches(row) == []
    assert json.loads(plan.world_bindings_json) == []


def test_final_tick_retires_settled_response_without_patching_it(pending_side_effect_session):
    session, row = pending_side_effect_session
    row.intent_ref = "settled"
    _response_lifecycle_nodes(session, row.project_id, {"settled": "resolved"})
    repo = WorldConflictRepository(session)
    repo.mark_resolved(row, decision=_decision("plot_responds"), result_refs=["hold"], pending_plan_response=True)
    result, deltas = _response_lifecycle_tick(session, row.project_id, final=True, interactions=[
        {"intent_id": "settled", "effect": "disrupted"},
    ])
    assert result.skipped_reason == "book_complete" and deltas == []
    assert repo.pending_plot_responses(row.project_id) == []
    assert json.loads(row.decision_json)["response_retired"] is True


# P4b quick wins: protected news, dead actors and superseded vetoes.


def test_direct_resolve_requeues_news_a_protection_forbids(monkeypatch) -> None:
    from forwin.planning.beat_sheet.types import ProtectionSpec

    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    faction = book.world.faction
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))
    # The faction stands where the held event happens, but may not know of it yet.
    deed = tick_event_fact_ref(book.project_id, f"conflict-{conflict_id}", "adjudication:held", 0)
    with Session.begin() as session:
        CanonAdmissionService().commit_plan_constraints(
            session=session, project_id=book.project_id, beat_sheet_id="sheet-hide-deed",
            creates=[ProtectionSpec(ref=faction, kind="must_not_know", until_chapter=0, observer_ref=faction,
                                    fact_ref=deed)],
            releases=[],
        )

    _resolve(Session, book, conflict_id, "accept_with_obligation")

    runtime = _runtime(Session, book)

    def knows(observer: str) -> str | None:
        views = [view for (_kind, observer_id), view in runtime.cognition_by_observer.items()
                 if observer_id == observer]
        return views[0].knowledge_by_ref.get(deed) if views else None

    # The hero took part; the faction's delivery waits in the queue for the next tick instead.
    assert (knows(book.world.hero), knows(faction)) == ("knows", None)
    [delta] = _resolution_deltas(Session, book, conflict_id)
    assert {"fact_ref": deed, "observer_ref": faction, "arrive_at": book.now + 1} in delta.metadata["news_in_transit"]
    engine.dispose()


def test_veto_does_not_reschedule_a_dead_actor(pending_side_effect_session):
    from tests.test_world_tick_steps import _actor, _intent, _runtime as _steps_runtime

    _, row = pending_side_effect_session
    row.intent_ref, row.actor_ref = "intent-1", "actor-1"

    def veto(actor: WorldNode) -> list[tuple[dict, dict]]:
        runtime = _steps_runtime(actor, _intent("intent-1", "actor-1", at=50))
        return [
            (item.intent_status_changes, item.next_turn_at)
            for item in option_candidates(row, _decision("veto_world"), runtime=runtime, policy=POLICY,
                                          reroll_count=0, now=100)
        ]

    assert veto(_actor("actor-1")) == [({"intent-1": "vetoed"}, {"actor-1": 100})]
    # A dead or inactive actor takes no more turns, so the veto only stops its intent.
    assert veto(_actor("actor-1", alive_status="dead")) == [({"intent-1": "vetoed"}, {})]
    assert veto(_actor("actor-1").model_copy(update={"is_active": False})) == [({"intent-1": "vetoed"}, {})]


def test_superseded_veto_does_not_cap_rerolls(pending_side_effect_session):
    from forwin.world_engine.actor_turns import recent_vetoes
    from forwin.world_engine.superseded_resolutions import supersede_resolutions

    session, retired = pending_side_effect_session
    repo = WorldConflictRepository(session)
    kept = repo.create(ConflictDraft(
        project_id=retired.project_id, source="world_tick", detected_after_chapter=1, story_time=50,
        involved_refs=[], violated="impact_review", impact_level="moderate", summary="kept", proposed_change={},
        actor_ref="actor", blocks_from_chapter=2,
    ), required_decider="human", options=ALL_OPTIONS)
    retired.actor_ref = "actor"
    repo.mark_resolved(retired, decision=_decision("veto_world"), result_refs=["retired-delta"])
    repo.mark_resolved(kept, decision=_decision("veto_world"), result_refs=["kept-delta"])
    assert repo.reroll_count(retired.project_id, actor_ref="actor") == 2

    supersede_resolutions(session, project_id=retired.project_id, graph_delta_id="retired-delta",
                          reason="historical rewrite")

    # The rewrite dropped the delta the veto committed through: the row stays resolved and names that delta,
    # but neither caps the actor's rerolls nor shows in its view any more.
    decision = json.loads(retired.decision_json)
    assert (retired.status, decision["option"], decision["superseded_by_delta"]) == (
        "resolved", "veto_world", "retired-delta",
    )
    assert repo.reroll_count(retired.project_id, actor_ref="actor") == 1
    assert recent_vetoes(session, retired.project_id, actor_ids=["actor"])["actor"] == [kept]
