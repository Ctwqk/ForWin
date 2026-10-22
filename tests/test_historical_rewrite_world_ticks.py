from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from forwin.audit.events import DecisionEventType
from forwin.book_state.projection import BookStateProjection
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.historical_rewrite import HistoricalCanonRewriteRepository
from forwin.canon.types import CanonAdmissionOutcome
from forwin.maintenance.events import ORDER_CONTROLS_KEY, POST_CANON_STEP_NAMES
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow, WorldNodeRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.project import ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import ApprovedGraphDeltaSet, CognitionPatch, GraphDelta, NodePatch
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_types import ConflictDecision, ConflictDraft
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE
from tests.postgres import postgres_test_url
from tests.test_canon_atomic_transaction import _prepare_chapter_candidate


GATE = "event-gate"
CAMP = "event-camp"
MEETING = "event-tick-meeting"
OBSERVER = ("character", "observer-scout")
FACT = "fact:fact-gate-secret"
TICK_2_EVIDENCE = "world_tick:tick-2"


@dataclass(frozen=True)
class _Book:
    Session: sessionmaker[Session]
    project_id: str
    chapter_ids: dict[int, str]
    commit_ids: dict[int, str]
    tick_2: str
    tick_3: str


@pytest.fixture
def Session() -> Iterator[sessionmaker[Session]]:
    engine = get_engine(postgres_test_url("historical-rewrite-world-ticks"))
    init_db(engine)
    yield get_session_factory(engine)
    engine.dispose()


def _create(project_id: str, node_id: str, *, description: str = "", **state: str) -> NodePatch:
    return NodePatch(
        node_id=node_id,
        node_type="event",
        op="create",
        new_value={
            "id": node_id, "project_id": project_id, "node_type": "event", "name": node_id,
            "description": description, "state": state,
        },
    )


def _set(node_id: str, field_path: str, old_value: str, new_value: str) -> NodePatch:
    return NodePatch(
        node_id=node_id, node_type="event", op="set", field_path=field_path,
        old_value=old_value, new_value=new_value,
    )


def _chapter_delta(project_id: str, chapter: int, delta_id: str, *patches: NodePatch) -> GraphDelta:
    return GraphDelta(
        id=delta_id, project_id=project_id, chapter_number=chapter, summary=delta_id, node_patches=list(patches),
    )


def _tick_delta(
    project_id: str,
    chapter: int,
    tick_id: str,
    *patches: NodePatch,
    cognition: list[CognitionPatch] | None = None,
    impacts: dict[str, str] | None = None,
) -> GraphDelta:
    return GraphDelta(
        id=f"world-tick:{project_id}:{tick_id}",
        project_id=project_id,
        chapter_number=chapter,
        story_time_start=chapter * 1000,
        story_time_end=chapter * 1000 + 60,
        source_type=WORLD_ENGINE_SOURCE_TYPE,
        source_id=tick_id,
        summary=f"tick {tick_id}",
        node_patches=list(patches),
        cognition_patches=cognition or [],
        evidence_refs=[f"world_tick:{tick_id}"],
        metadata={"tick_id": tick_id, "source_chapter": chapter, "candidate_impacts": impacts or {}},
    )


def _accept(Session, project_id: str, chapter_number: int, *, version: int, deltas: list[GraphDelta]) -> tuple[str, str]:
    with Session.begin() as session:
        chapter = session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == chapter_number,
        ))
        plan = _prepare_chapter_candidate(
            session, project_id=project_id, chapter=chapter, version=version,
            body=f"Chapter {chapter_number} version {version}.", summary=f"Chapter {chapter_number}.",
            delta_id="", delta_summary="", node_id="", graph_deltas=deltas,
        )
    outcome = CanonAdmissionService(session_factory=Session).commit_plan(plan)
    assert outcome.blocked is False, outcome.failure_reason
    return outcome.commit_id, plan.candidate_id


def _complete_post_canon(
    Session, project_id: str, chapter_number: int, commit: tuple[str, str], *, world_delta_ids: tuple[str, ...],
) -> None:
    """Every post-Canon step succeeded; the world step lists the tick's deltas as Task 13 will."""
    commit_id, candidate_id = commit
    with Session.begin() as session:
        for step_name in POST_CANON_STEP_NAMES:
            result: dict = {}
            if step_name == "world":
                result["graph_delta_ids"] = list(world_delta_ids)
            if step_name == POST_CANON_STEP_NAMES[-1]:
                result[ORDER_CONTROLS_KEY] = {"status": "succeeded", "result": {"blocking_reasons": []}}
            session.add(PostCanonMaintenanceRun(
                id=f"post-canon-{commit_id}-{step_name}",
                canon_commit_id=commit_id,
                project_id=project_id,
                chapter_number=chapter_number,
                candidate_id=candidate_id,
                step_name=step_name,
                idempotency_key=f"post-canon-key-{commit_id}-{step_name}",
                status="succeeded",
                result_json=json.dumps(result, sort_keys=True),
            ))


def _commit_tick(Session, delta: GraphDelta, *, tick_id: str | None = None):
    with Session.begin() as session:
        return CanonAdmissionService().commit_world_tick(
            session=session,
            project_id=delta.project_id,
            chapter_number=delta.chapter_number,
            approved_changes=ApprovedGraphDeltaSet(
                project_id=delta.project_id, chapter_number=delta.chapter_number, graph_deltas=[delta],
            ),
            tick_id=tick_id or delta.metadata["tick_id"],
        )


def _tick_three(project_id: str) -> GraphDelta:
    return _tick_delta(project_id, 3, "tick-3", _set(CAMP, "state.mood", "calm", "angry"), impacts={"c-1": "minor"})


def _book(Session, *, list_chapter_two_tick: bool = True) -> _Book:
    """Chapters 1-3 are Canon; a world tick ran after chapter 2 and after chapter 3.

    The chapter 2 tick opens the gate, renames it, convenes a meeting and tells the scout a
    secret; the chapter 3 tick angers the camp.
    """
    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="Ticking world", premise="The world moves between chapters.", genre="test",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
        arc = updater.create_arc_plan(project.id, "Tick arc")
        chapter_ids = {
            number: updater.create_chapter_plan(
                project.id, arc.id, number, f"Chapter {number}", f"Chapter {number} event", [f"Commit chapter {number}"],
            ).id
            for number in (1, 2, 3)
        }
        project_id = project.id
    commits = {1: _accept(Session, project_id, 1, version=1, deltas=[_chapter_delta(
        project_id, 1, "delta-ch1",
        _create(project_id, GATE, description="gate description", phase="closed"),
        _create(project_id, CAMP, mood="calm"),
    )])}
    _complete_post_canon(Session, project_id, 1, commits[1], world_delta_ids=())
    commits[2] = _accept(Session, project_id, 2, version=1, deltas=[
        _chapter_delta(project_id, 2, "delta-ch2-original", _create(project_id, "event-original")),
    ])
    tick_2 = _commit_tick(Session, _tick_delta(
        project_id, 2, "tick-2",
        _set(GATE, "state.phase", "closed", "open"),
        _set(GATE, "description", "gate description", "gate opened"),
        _create(project_id, MEETING, phase="convened"),
        cognition=[CognitionPatch(
            observer_type=OBSERVER[0], observer_id=OBSERVER[1], op="merge", field_path="knowledge_by_ref",
            old_value={FACT: None}, new_value={FACT: "knows"}, evidence_refs=[TICK_2_EVIDENCE],
        )],
        impacts={"c-1": "minor", "c-2": "major", "c-3": "moderate"},
    )).graph_delta_ids
    _complete_post_canon(
        Session, project_id, 2, commits[2], world_delta_ids=tick_2 if list_chapter_two_tick else (),
    )
    commits[3] = _accept(Session, project_id, 3, version=1, deltas=[
        _chapter_delta(project_id, 3, "delta-ch3", _create(project_id, "event-later")),
    ])
    tick_3 = _commit_tick(Session, _tick_three(project_id)).graph_delta_ids
    _complete_post_canon(Session, project_id, 3, commits[3], world_delta_ids=tick_3)
    return _Book(
        Session=Session,
        project_id=project_id,
        chapter_ids=chapter_ids,
        commit_ids={number: commit_id for number, (commit_id, _) in commits.items()},
        tick_2=tick_2[0],
        tick_3=tick_3[0],
    )


def _rewrite_chapter_two(book: _Book, *, version: int, delta: GraphDelta) -> CanonAdmissionOutcome:
    with book.Session.begin() as session:
        HistoricalCanonRewriteRepository(session).mark_pending(
            project_id=book.project_id, chapter_number=2, reason="correct chapter two",
        )
        chapter = session.get(ChapterPlan, book.chapter_ids[2])
        chapter.status = "planned"
        session.flush()
        plan = _prepare_chapter_candidate(
            session, project_id=book.project_id, chapter=chapter, version=version,
            body=f"Chapter 2 version {version}.", summary="Chapter 2 rewritten.",
            delta_id="", delta_summary="", node_id="", graph_deltas=[delta],
        )
    return CanonAdmissionService(session_factory=book.Session).commit_plan(plan)


def _quiet_rewrite(book: _Book, version: int) -> CanonAdmissionOutcome:
    """A chapter 2 rewrite that leaves every value the ticks read alone."""
    return _rewrite_chapter_two(book, version=version, delta=_chapter_delta(
        book.project_id, 2, f"delta-ch2-v{version}", _create(book.project_id, f"event-v{version}"),
    ))


def _sealing_rewrite(book: _Book, version: int) -> CanonAdmissionOutcome:
    """A chapter 2 rewrite that seals the gate the chapter 2 tick opens."""
    return _rewrite_chapter_two(book, version=version, delta=_chapter_delta(
        book.project_id, 2, f"delta-ch2-v{version}", _set(GATE, "state.phase", "closed", "sealed"),
    ))


def _runtime(book: _Book, chapter: int):
    with book.Session() as session:
        return BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=chapter)


def _state(book: _Book, chapter: int, node_id: str) -> dict:
    return _runtime(book, chapter).world.get_state(node_id)


def _knowledge(book: _Book, chapter: int) -> tuple[dict, dict]:
    view = _runtime(book, chapter).cognition_by_observer.get(OBSERVER)
    if view is None:
        return {}, {}
    return dict(view.knowledge_by_ref), dict(view.evidence_by_ref)


def _conflicts(book: _Book) -> list[WorldPlanConflictRow]:
    with book.Session() as session:
        return WorldConflictRepository(session).list(book.project_id)


def _delta_ids(book: _Book) -> set[str]:
    with book.Session() as session:
        return set(session.scalars(select(GraphDeltaRow.id).where(GraphDeltaRow.project_id == book.project_id)))


def _tick_delta_ids_by_tick(book: _Book) -> dict[str, list[str]]:
    with book.Session() as session:
        rows = session.execute(select(GraphDeltaRow.id, GraphDeltaRow.metadata_json).where(
            GraphDeltaRow.project_id == book.project_id, GraphDeltaRow.source_type == WORLD_ENGINE_SOURCE_TYPE,
        )).all()
    by_tick: dict[str, list[str]] = {}
    for delta_id, metadata_json in rows:
        by_tick.setdefault(json.loads(metadata_json)["tick_id"], []).append(delta_id)
    return by_tick


def test_rewrite_keeps_listed_tick_deltas(Session) -> None:
    book = _book(Session)

    outcome = _quiet_rewrite(book, 2)

    assert outcome.blocked is False, outcome.failure_reason
    assert {book.tick_2, book.tick_3} <= _delta_ids(book)
    for chapter in (2, 3):
        assert _state(book, chapter, GATE)["phase"] == "open"
        assert _state(book, chapter, MEETING)["phase"] == "convened"
        # The retained tick's cognition lands once, after the replacement chapter.
        assert _knowledge(book, chapter) == ({FACT: "knows"}, {FACT: [TICK_2_EVIDENCE]})
    assert _state(book, 2, CAMP)["mood"] == "calm"
    assert _state(book, 3, CAMP)["mood"] == "angry"
    assert "event-v2" in _runtime(book, 3).world.nodes_by_id
    with book.Session() as session:
        assert session.get(WorldNodeRow, GATE).description == "gate opened"
        assert session.get(WorldNodeRow, MEETING) is not None
        assert session.get(WorldNodeRow, "event-original") is None
    assert _conflicts(book) == []


def test_rewrite_replays_tick_deltas_and_turns_cas_failure_into_conflict(Session) -> None:
    book = _book(Session)

    outcome = _sealing_rewrite(book, 2)

    assert outcome.blocked is False, outcome.failure_reason
    # The chapter 2 tick read an open-able gate: all of it stops taking effect.
    assert book.tick_2 not in _delta_ids(book)
    for chapter in (2, 3):
        assert _state(book, chapter, GATE)["phase"] == "sealed"
        assert MEETING not in _runtime(book, chapter).world.nodes_by_id
        assert _knowledge(book, chapter) == ({}, {})
    with book.Session() as session:
        # The rewind restored the field the tick renamed and removed the node it created.
        assert session.get(WorldNodeRow, GATE).description == "gate description"
        assert session.get(WorldNodeRow, MEETING) is None
    # The other deltas take effect as before.
    assert book.tick_3 in _delta_ids(book)
    assert _state(book, 3, CAMP)["mood"] == "angry"
    assert "event-later" in _runtime(book, 3).world.nodes_by_id

    [conflict] = _conflicts(book)
    assert (
        conflict.status, conflict.source, conflict.violated, conflict.required_decider, conflict.impact_level,
    ) == ("open", "historical_rewrite", "replay_cas", "human", "major")
    assert json.loads(conflict.proposed_change_json) == {"graph_delta_id": book.tick_2, "tick_id": "tick-2"}
    assert json.loads(conflict.options_json) == ["veto_world"]
    assert (conflict.detected_after_chapter, conflict.story_time, conflict.blocks_from_chapter) == (2, 2060, 4)
    assert json.loads(conflict.involved_refs_json) == [GATE]
    with book.Session() as session:
        repository = WorldConflictRepository(session)
        assert repository.open_blocking(book.project_id, next_chapter=4).id == conflict.id
        assert repository.open_blocking(book.project_id, next_chapter=3) is None
        [event] = session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == book.project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_DETECTED,
        )).all()
    payload = json.loads(event.payload_json)
    assert (event.related_object_type, event.related_object_id) == ("world_conflict", conflict.id)
    assert (payload["conflict_id"], payload["graph_delta_id"], payload["tick_id"]) == (
        conflict.id, book.tick_2, "tick-2",
    )
    # The dropped delta leaves the ledger, so the event keeps what it was.
    assert payload["graph_delta"]["id"] == book.tick_2
    assert [mismatch["patch"] for mismatch in payload["cas_mismatches"]] == [f"node:{GATE}:state.phase"]


@pytest.mark.parametrize("authorization", ["unlisted", "failed_world_step", "open_conflict"])
def test_unlisted_world_engine_delta_still_invalidates_rewrite(Session, authorization: str) -> None:
    book = _book(Session, list_chapter_two_tick=authorization == "failed_world_step")
    with Session.begin() as session:
        if authorization == "failed_world_step":
            session.execute(update(PostCanonMaintenanceRun).where(
                PostCanonMaintenanceRun.canon_commit_id == book.commit_ids[2],
                PostCanonMaintenanceRun.step_name == "world",
            ).values(status="failed"))
        if authorization == "open_conflict":
            # Only a decided conflict vouches for the deltas it lists.
            conflict = _conflict(session, book)
            conflict.result_refs_json = json.dumps([book.tick_2])

    outcome = _quiet_rewrite(book, 2)

    assert outcome.stale is True
    assert "historical rewrite delta manifest missing or invalid" in outcome.failure_reason
    assert book.tick_2 in _delta_ids(book)
    assert _state(book, 3, GATE)["phase"] == "open"
    with Session() as session:
        assert session.get(CanonCommitRecord, book.commit_ids[2]).status == "committed"
    assert [conflict.violated for conflict in _conflicts(book)] == (
        ["protection"] if authorization == "open_conflict" else []
    )


def _conflict(session: Session, book: _Book) -> WorldPlanConflictRow:
    return WorldConflictRepository(session).create(
        ConflictDraft(
            project_id=book.project_id, source="world_tick", detected_after_chapter=2, story_time=0,
            involved_refs=[], violated="protection", impact_level="major", summary="held tick change",
            proposed_change={}, blocks_from_chapter=3,
        ),
        required_decider="human",
        options=("veto_world",),
    )


def test_tick_delta_listed_by_a_resolved_conflict_is_retained(Session) -> None:
    book = _book(Session, list_chapter_two_tick=False)
    with Session.begin() as session:
        repository = WorldConflictRepository(session)
        repository.mark_resolved(
            _conflict(session, book),
            decision=ConflictDecision(
                option="accept_with_obligation", decided_by="human", rationale="keep it",
                decided_at=datetime.now(UTC),
            ),
            result_refs=[book.tick_2],
        )

    outcome = _quiet_rewrite(book, 2)

    assert outcome.blocked is False, outcome.failure_reason
    assert _state(book, 3, GATE)["phase"] == "open"


def test_commit_world_tick_stays_idempotent_after_rewrite(Session) -> None:
    book = _book(Session)
    assert _quiet_rewrite(book, 2).blocked is False

    # Replay kept each tick delta's id, so a retried tick finds the one it committed.
    again = _commit_tick(
        Session, _tick_three(book.project_id).model_copy(update={"id": "rebuilt-tick-3"}), tick_id="tick-3",
    )

    assert (again.graph_delta_ids, again.idempotent) == ((book.tick_3,), True)
    assert _tick_delta_ids_by_tick(book) == {"tick-2": [book.tick_2], "tick-3": [book.tick_3]}


def test_later_rewrite_accepts_ticks_listed_under_the_superseded_commit(Session) -> None:
    book = _book(Session)
    assert _quiet_rewrite(book, 2).blocked is False

    # The chapter 2 tick ran after the commit the first rewrite superseded.
    outcome = _quiet_rewrite(book, 3)

    assert outcome.blocked is False, outcome.failure_reason
    assert _state(book, 3, GATE)["phase"] == "open"
    assert _knowledge(book, 3) == ({FACT: "knows"}, {FACT: [TICK_2_EVIDENCE]})
    assert _conflicts(book) == []


def test_later_rewrite_neither_rejects_nor_reapplies_a_dropped_tick_delta(Session) -> None:
    book = _book(Session)
    assert _sealing_rewrite(book, 2).blocked is False

    outcome = _quiet_rewrite(book, 3)

    assert outcome.blocked is False, outcome.failure_reason
    assert book.tick_2 not in _delta_ids(book)
    for chapter in (2, 3):
        assert _state(book, chapter, GATE)["phase"] == "closed"
        assert MEETING not in _runtime(book, chapter).world.nodes_by_id
    assert _state(book, 3, CAMP)["mood"] == "angry"
    assert len(_conflicts(book)) == 1
