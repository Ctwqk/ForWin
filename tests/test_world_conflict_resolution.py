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
from forwin.protocol.book_state import GraphDelta, NarrativePatch
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
                impact_level=impact, summary=SUMMARY, proposed_change=proposed_change, intent_ref=intent_ref,
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

    unreadable = _conflict(Session, book, proposed_change={"patches": []}, options=["veto_world", "plot_responds"])
    _resolve_failing(Session, book, unreadable, "plot_responds", ConflictOptionUnavailable)
    _resolve(Session, book, unreadable, "veto_world")
    assert _row(Session, book, unreadable) == ("resolved", [], "veto_world")
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
    # Ending a relation that does not exist only warns; the rest of the change still lands.
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
    assert [warning["code"] for warning in result.warnings] == ["relation_missing"]
    [(_actor_type, payload)] = _resolved_events(Session, book, conflict_id)
    assert payload["warnings"] == [dict(warning) for warning in result.warnings]
    assert set(payload["warnings"][0]) == {"code", "message"}

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
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "resolved"
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
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "resolved"
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
        candidate_id="adjudication:kill", origin="adjudication", actor_id=book.world.hero, summary="summary-kill",
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

    result = _resolve(Session, book, conflict_id, "plot_responds")

    assert result.graph_delta_ids == (_delta_id(book, conflict_id),)
    assert _runtime(Session, book).world.get_state(book.world.hero)["location_id"] == book.world.loc(2)
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
    assert _conflict_row(Session, world.project_id, conflict_id) == ("open", [])
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
    assert repo.pending_plot_responses(row.project_id) == []


def test_accept_with_obligation_without_next_plan_records_obligation_only(pending_side_effect_session):
    from forwin.world_engine.conflict_resolution import record_option_side_effects

    session, row = pending_side_effect_session
    effects = record_option_side_effects(session, row, _decision("accept_with_obligation"), latest_chapter=1)
    assert effects.plan_patch_id == ""
    obligation = session.get(NarrativeObligationRow, effects.obligation_id)
    assert obligation.status == "proposed"
    assert obligation.deadline_chapter == 7
    assert json.loads(obligation.linked_plan_patch_ids_json) == []
    assert session.scalars(select(NarrativePlanPatchRow)).all() == []


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
    from forwin.world_engine.tick import _onstage_plan_bindings

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
    assert _onstage_plan_bindings(session, project_id, 4) == (
        frozenset({"current"}), True, frozenset({"earlier", "current"}),
    )
    band.status = "superseded"
    assert _onstage_plan_bindings(session, project_id, 4)[1] is False
    assert _onstage_plan_bindings(session, project_id, 8) == (
        frozenset(), True, frozenset({"earlier", "current"}),
    )
    sheet.status = "superseded"
    assert _onstage_plan_bindings(session, project_id, 8) == (frozenset(), False, frozenset())
