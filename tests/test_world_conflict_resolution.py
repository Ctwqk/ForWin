from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.runtime import BookStateRuntime
from forwin.canon import CanonAdmissionService, CanonWriteFailure
from forwin.models import ArcPlanVersion, ChapterPlan, NarrativeObligationRow, NarrativePlanPatchRow, Project
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import GraphDeltaRow
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
    EffectRelationChange,
    NewsItem,
    TickCandidate,
    WorldEffects,
)
from forwin.writer.prompt_core.world_engine import plot_response_goal
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
        assert (
            obligation.obligation_type, obligation.summary, obligation.payoff_test, obligation.deadline_chapter,
            obligation.origin_chapter_number, obligation.status,
        ) == ("world_change_response", SUMMARY, SUMMARY, 7, 1, "planned")
        patch = session.get(NarrativePlanPatchRow, result.plan_patch_id)
        assert (patch.target_plan_id, patch.applied, json.loads(patch.affected_chapters_json)) == (
            book.plans[2], True, [2],
        )
        assert json.loads(obligation.linked_plan_patch_ids_json) == [result.plan_patch_id]
    goals, _experience = _plan(Session, book.plans[2])
    assert any(result.obligation_id in goal and SUMMARY in goal for goal in goals)
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
    goal = plot_response_goal(SUMMARY)
    assert SUMMARY in goal and goal != SUMMARY
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


def test_plot_responds_without_an_unwritten_plan_is_unavailable(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    with Session.begin() as session:
        for number in (2, 3):
            session.get(ChapterPlan, book.plans[number]).status = "accepted"
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))

    _resolve_failing(Session, book, conflict_id, "plot_responds", ConflictOptionUnavailable)

    assert _resolution_deltas(Session, book, conflict_id) == []
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
    ] == [("adjudication:kill", {}), (f"conflict:{conflict_id}:release", {"protect-rival": 1})]

    result = _resolve(Session, book, conflict_id, "amend_plan")

    delta_id = _delta_id(book, conflict_id)
    assert result == ConflictResolutionResult(graph_delta_ids=(delta_id,), obligation_id="", plan_patch_id="")
    [delta] = _resolution_deltas(Session, book, conflict_id)
    # The release rides in the same delta as the change, with the protection's old until_chapter.
    assert [(patch.target_ref, str(patch.op), patch.field_path, patch.old_value, patch.new_value)
            for patch in delta.narrative_patches] == [
        ("review_constraint:protect-rival", "set", "payload.until_chapter", 0, 1),
    ]
    runtime = _runtime(Session, book)
    assert runtime.world.get_state(rival)["alive_status"] == "dead"
    assert runtime.narrative.nodes_by_id["protect-rival"].payload["until_chapter"] == 1
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
    assert _plan(Session, book.plans[2])[0] == [plot_response_goal(SUMMARY), "goal-2"]
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


def test_stale_held_change_keeps_conflict_open(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))
    # A concurrent tick disrupts the intent the held change resolves.
    racing_tick = _built(Session, _context(book.world, book.chapter), [TickCandidate(
        candidate_id="race", origin="adjudication", summary="summary-race", effects=WorldEffects(),
        intent_status_changes={book.later: "disrupted"},
    )], tick_id="tick-race")

    def race() -> None:
        _commit_tick(Session, book.world, racing_tick, tick_id="tick-race")

    _resolve_failing(Session, book, conflict_id, "plot_responds", ConflictStale, admission=_RacingAdmission(race))

    assert _row(Session, book, conflict_id) == ("open", [], "")
    assert _resolution_deltas(Session, book, conflict_id) == []
    assert _resolved_events(Session, book, conflict_id) == []
    assert _plan(Session, book.plans[2])[0] == ["goal-2"]
    assert _runtime(Session, book).world.get_state(book.later)["status"] == "disrupted"
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


def test_a_chapter_committed_under_the_resolution_makes_it_stale(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch)
    conflict_id = _conflict(Session, book, proposed_change=_held(book).model_dump(mode="json"))
    chapter_two = _delta(Session, book.world, 2, _time(1, "日"))

    def race() -> None:
        _commit(Session, book.world, chapter_two)

    _resolve_failing(Session, book, conflict_id, "plot_responds", ConflictStale, admission=_RacingAdmission(race))
    assert _row(Session, book, conflict_id) == ("open", [], "")
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


def test_builder_releases_only_review_constraints_and_keeps_an_absent_until_chapter(monkeypatch) -> None:
    engine, Session = _session_factory()
    book = _book(Session, monkeypatch, protections=lambda world: [
        ("protect-open", {"ref": world.rival, "kind": "must_be_alive", "source": "test"}),
    ])
    context = _context(book.world, book.chapter)

    def release(ref: str) -> TickCandidate:
        return TickCandidate(
            candidate_id="c-release", origin="conflict_resolution", summary=SUMMARY, effects=WorldEffects(),
            protection_releases={ref: 1},
        )

    unknown = _build(Session, context, [release("not-a-protection")], tick_id="tick-release")
    assert unknown.delta is None
    assert _issues(unknown) == [("error", "unresolved_ref", "c-release")]
    built = _built(Session, context, [release("protect-open")], tick_id="tick-release")
    assert [(patch.target_ref, patch.field_path, patch.old_value, patch.new_value)
            for patch in built.narrative_patches] == [
        ("review_constraint:protect-open", "payload.until_chapter", None, 1),
    ]
    engine.dispose()
