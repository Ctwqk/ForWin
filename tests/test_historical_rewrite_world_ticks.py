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
from forwin.models.book_state import GraphDeltaRow, WorldEdgeRow, WorldNodeRow, WorldSnapshotRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.narrative_obligation import NarrativeObligationRow, NarrativePlanPatchRow
from forwin.models.project import ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.models.draft import CandidateDraftRecord
from forwin.planning.goals import load_goals_json
from forwin.protocol.book_state import ApprovedGraphDeltaSet, CognitionPatch, EdgePatch, GraphDelta, NodePatch
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_resolution import conflict_resolved_event, record_option_side_effects
from forwin.world_engine.conflict_types import ConflictDecision, ConflictDraft, ConflictOption
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE
from forwin.writer.prompt_core.world_engine import world_change_response
from tests.postgres import postgres_test_url
from tests.test_canon_atomic_transaction import _prepare_chapter_candidate


GATE = "event-gate"
CAMP = "event-camp"
MEETING = "event-tick-meeting"
OBSERVER = ("character", "observer-scout")
FACT = "fact:fact-gate-secret"
TICK_2_EVIDENCE = "world_tick:tick-2"
# The writer-visible goal of an accepted change with no public event, as these held changes are.
RESPONSE_GOAL = world_change_response([], onstage_slip=False)


@dataclass(frozen=True)
class _Book:
    Session: sessionmaker[Session]
    project_id: str
    chapter_ids: dict[int, str]
    commit_ids: dict[int, str]
    tick_2: str
    tick_3: str
    # The conflicts each chapter's tick resolved in-tick, by the option decided.
    resolved: dict[int, dict[str, str]]


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


def _chapter_delta(project_id: str, chapter: int, delta_id: str, *patches: NodePatch | EdgePatch) -> GraphDelta:
    return GraphDelta(
        id=delta_id, project_id=project_id, chapter_number=chapter, summary=delta_id,
        node_patches=[patch for patch in patches if isinstance(patch, NodePatch)],
        edge_patches=[patch for patch in patches if isinstance(patch, EdgePatch)],
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


def _tick_three(project_id: str, *patches: NodePatch) -> GraphDelta:
    return _tick_delta(
        project_id, 3, "tick-3", _set(CAMP, "state.mood", "calm", "angry"), *patches, impacts={"c-1": "minor"},
    )


def _resolve_in_tick(
    Session, project_id: str, chapter: int, delta_id: str, options: tuple[ConflictOption, ...],
) -> dict[str, str]:
    """Conflicts the chapter's tick decided in-tick, recorded the way its close_resolutions records them."""
    resolved: dict[str, str] = {}
    with Session.begin() as session:
        repository = WorldConflictRepository(session)
        for option in options:
            row = repository.create(
                ConflictDraft(
                    project_id=project_id, source="world_tick", detected_after_chapter=chapter, story_time=0,
                    involved_refs=[CAMP], violated="impact_review", impact_level="major",
                    summary=f"world change {chapter} {option}", proposed_change={}, blocks_from_chapter=chapter + 1,
                ),
                required_decider="codex",
                options=(option,),
            )
            decision = ConflictDecision(
                option=option, decided_by="codex", rationale="decided in tick", decided_at=datetime.now(UTC),
            )
            effects = record_option_side_effects(session, row, decision, latest_chapter=chapter)
            refs = [delta_id, effects.obligation_id, effects.plan_patch_id]
            repository.mark_resolved(row, decision=decision, result_refs=[ref for ref in refs if ref])
            session.add(conflict_resolved_event(row, chapter_number=chapter, extra={
                "graph_delta_ids": [delta_id],
                "obligation_id": effects.obligation_id,
                "plan_patch_id": effects.plan_patch_id,
            }))
            resolved[option] = row.id
    return resolved


def _book(
    Session,
    *,
    list_chapter_two_tick: bool = True,
    chapter_three: tuple[NodePatch | EdgePatch, ...] = (),
    tick_three: tuple[NodePatch, ...] = (),
    tick_two_resolutions: tuple[ConflictOption, ...] = (),
    tick_three_resolutions: tuple[ConflictOption, ...] = (),
) -> _Book:
    """Chapters 1-3 are Canon and chapter 4 is still to write; a world tick ran after chapter 2 and after chapter 3.

    The chapter 2 tick opens the gate, renames it, convenes a meeting and tells the scout a
    secret; the chapter 3 tick angers the camp. ``chapter_three`` and ``tick_three`` add
    patches to chapter 3's own delta and to its tick. Each tick also decides one conflict per
    option in its ``*_resolutions``, whose side effects bind to the next chapter's plan.
    """
    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="Ticking world", premise="The world moves between chapters.", genre="test",
            runtime_policy=RuntimePolicy.for_profile("standard"), target_total_chapters=10,
        )
        arc = updater.create_arc_plan(project.id, "Tick arc")
        chapter_ids = {
            number: updater.create_chapter_plan(
                project.id, arc.id, number, f"Chapter {number}", f"Chapter {number} event", [f"Commit chapter {number}"],
            ).id
            for number in (1, 2, 3, 4)
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
    resolved = {2: _resolve_in_tick(Session, project_id, 2, tick_2[0], tick_two_resolutions)}
    _complete_post_canon(
        Session, project_id, 2, commits[2], world_delta_ids=tick_2 if list_chapter_two_tick else (),
    )
    # An edge create carries its project in the payload.
    chapter_three = tuple(
        patch.model_copy(update={"new_value": {**patch.new_value, "project_id": project_id}})
        if isinstance(patch, EdgePatch) else patch
        for patch in chapter_three
    )
    commits[3] = _accept(Session, project_id, 3, version=1, deltas=[
        _chapter_delta(project_id, 3, "delta-ch3", _create(project_id, "event-later"), *chapter_three),
    ])
    tick_3 = _commit_tick(Session, _tick_three(project_id, *tick_three)).graph_delta_ids
    resolved[3] = _resolve_in_tick(Session, project_id, 3, tick_3[0], tick_three_resolutions)
    _complete_post_canon(Session, project_id, 3, commits[3], world_delta_ids=tick_3)
    return _Book(
        Session=Session,
        project_id=project_id,
        chapter_ids=chapter_ids,
        commit_ids={number: commit_id for number, (commit_id, _) in commits.items()},
        tick_2=tick_2[0],
        tick_3=tick_3[0],
        resolved=resolved,
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


def _replay_change(
    delta_id: str,
    tick_id: str,
    *,
    superseded_conflict_ids: list[str] | None = None,
    cancelled_obligation_ids: list[str] | None = None,
    reverted_plan_patch_ids: list[str] | None = None,
) -> dict:
    """A replay_cas conflict's proposed_change."""
    return {
        "graph_delta_id": delta_id,
        "tick_id": tick_id,
        "superseded_conflict_ids": superseded_conflict_ids or [],
        "cancelled_obligation_ids": cancelled_obligation_ids or [],
        "reverted_plan_patch_ids": reverted_plan_patch_ids or [],
    }


def _replay_conflict(book: _Book) -> WorldPlanConflictRow:
    [conflict] = [conflict for conflict in _conflicts(book) if conflict.source == "historical_rewrite"]
    return conflict


def _side_effects(book: _Book, conflict_id: str) -> tuple[str, str]:
    """The obligation (or "") and plan patch an in-tick resolution recorded, from its RESOLVED event."""
    with book.Session() as session:
        payload = json.loads(session.scalar(select(DecisionEvent.payload_json).where(
            DecisionEvent.related_object_id == conflict_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_RESOLVED,
        )))
    return payload["obligation_id"], payload["plan_patch_id"]


def _plan(book: _Book, chapter: int) -> tuple[str, list[str], dict]:
    with book.Session() as session:
        plan = session.get(ChapterPlan, book.chapter_ids[chapter])
        return plan.status, load_goals_json(plan.goals_json), json.loads(plan.experience_plan_json)


def _obligation(book: _Book, obligation_id: str) -> tuple[str, dict]:
    with book.Session() as session:
        row = session.get(NarrativeObligationRow, obligation_id)
        return row.status, json.loads(row.metadata_json)


def _revert_patches(book: _Book) -> list[NarrativePlanPatchRow]:
    with book.Session() as session:
        return list(session.scalars(select(NarrativePlanPatchRow).where(
            NarrativePlanPatchRow.project_id == book.project_id,
            NarrativePlanPatchRow.patch_type == "plan_patch_revert",
        )))


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
    assert json.loads(conflict.proposed_change_json) == _replay_change(book.tick_2, "tick-2")
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
    assert (event.related_object_type, event.related_object_id, event.chapter_number) == (
        "world_conflict", conflict.id, 2,
    )
    assert {key: payload[key] for key in (
        "conflict_id", "source", "violated", "impact_level", "required_decider", "blocks_from_chapter",
        "proposed_change", "rewritten_chapter", "dropped_dependency",
    )} == {
        "conflict_id": conflict.id,
        "source": "historical_rewrite",
        "violated": "replay_cas",
        "impact_level": "major",
        "required_decider": "human",
        "blocks_from_chapter": 4,
        "proposed_change": _replay_change(book.tick_2, "tick-2"),
        "rewritten_chapter": 2,
        "dropped_dependency": None,
    }
    # The dropped delta leaves the ledger, so the event keeps what it was.
    assert payload["graph_delta"]["id"] == book.tick_2
    assert payload["cas_mismatches"] == [
        {"patch": f"node:{GATE}:state.phase", "expected": "closed", "current": "sealed"},
    ]


@pytest.mark.parametrize(
    "authorization",
    ["unlisted", "failed_world_step", "other_chapter_world_step", "non_world_step", "open_conflict"],
)
def test_unlisted_world_engine_delta_still_invalidates_rewrite(Session, authorization: str) -> None:
    book = _book(Session, list_chapter_two_tick=authorization == "failed_world_step")
    with Session.begin() as session:
        if authorization == "failed_world_step":
            session.execute(update(PostCanonMaintenanceRun).where(
                PostCanonMaintenanceRun.canon_commit_id == book.commit_ids[2],
                PostCanonMaintenanceRun.step_name == "world",
            ).values(status="failed"))
        # Only a world step of the tick's own chapter lists its deltas.
        if authorization == "other_chapter_world_step":
            session.execute(update(PostCanonMaintenanceRun).where(
                PostCanonMaintenanceRun.canon_commit_id == book.commit_ids[3],
                PostCanonMaintenanceRun.step_name == "world",
            ).values(result_json=json.dumps({"graph_delta_ids": [book.tick_3, book.tick_2]})))
        if authorization == "non_world_step":
            session.execute(update(PostCanonMaintenanceRun).where(
                PostCanonMaintenanceRun.canon_commit_id == book.commit_ids[2],
                PostCanonMaintenanceRun.step_name != "world",
            ).values(result_json=json.dumps({"graph_delta_ids": [book.tick_2]})))
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


def _outcome_snapshot(book: _Book) -> tuple:
    with book.Session() as session:
        return (
            session.get(CanonCommitRecord, book.commit_ids[2]).status,
            session.get(WorldNodeRow, MEETING) is not None,
            sorted(session.scalars(select(WorldEdgeRow.id).where(WorldEdgeRow.project_id == book.project_id))),
        )


@pytest.mark.parametrize(
    "dependent",
    [
        _set(MEETING, "state.phase", "convened", "adjourned"),
        EdgePatch(
            edge_id="edge-camp-meeting", op="create", source_id=CAMP, target_id=MEETING,
            edge_type="ally_of", edge_family="social", new_value={},
        ),
    ],
    ids=["sets_a_field", "creates_an_edge"],
)
def test_chapter_delta_depending_on_a_dropped_tick_aborts_rewrite(Session, dependent) -> None:
    # Chapter 3 acts on the meeting that the chapter 2 tick convened.
    book = _book(Session, chapter_three=(dependent,))
    before = _outcome_snapshot(book)

    outcome = _sealing_rewrite(book, 2)

    assert outcome.stale is True
    assert book.tick_2 in outcome.failure_reason
    assert "delta-ch3" in outcome.failure_reason
    # Nothing of the rewrite is kept: no dangling edge, no conflict, the old commit stands.
    assert _outcome_snapshot(book) == before
    assert before[0] == "committed"
    assert book.tick_2 in _delta_ids(book)
    assert _conflicts(book) == []
    with Session() as session:
        candidate = session.scalar(select(CandidateDraftRecord).where(
            CandidateDraftRecord.project_id == book.project_id,
            CandidateDraftRecord.chapter_number == 2,
            CandidateDraftRecord.status == "ready_for_canon",
        ))
    assert candidate is not None


def test_tick_depending_on_a_dropped_tick_cascades(Session) -> None:
    # The chapter 3 tick sets a field the meeting does not have yet: its CAS alone passes.
    unset_mood = NodePatch(
        node_id=MEETING, node_type="event", op="set", field_path="state.mood", old_value=None, new_value="tense",
    )
    book = _book(Session, tick_three=(unset_mood,))

    outcome = _sealing_rewrite(book, 2)

    assert outcome.blocked is False, outcome.failure_reason
    assert not {book.tick_2, book.tick_3} & _delta_ids(book)
    assert _state(book, 3, CAMP)["mood"] == "calm"
    assert MEETING not in _runtime(book, 3).world.nodes_by_id
    conflicts = {json.loads(conflict.proposed_change_json)["graph_delta_id"]: conflict for conflict in _conflicts(book)}
    assert set(conflicts) == {book.tick_2, book.tick_3}
    cascaded = conflicts[book.tick_3]
    assert (cascaded.violated, cascaded.required_decider, cascaded.detected_after_chapter) == ("replay_cas", "human", 3)
    assert json.loads(cascaded.involved_refs_json) == [MEETING]
    with Session() as session:
        payload = json.loads(session.scalar(select(DecisionEvent.payload_json).where(
            DecisionEvent.related_object_id == cascaded.id,
        )))
    assert payload["dropped_dependency"] == {"ref": MEETING, "graph_delta_id": book.tick_2}
    assert payload["cas_mismatches"] == []


def test_rewrite_drops_a_successor_chapter_tick_whose_reads_changed(Session) -> None:
    book = _book(Session)

    # The rewritten chapter 2 already moves the camp the chapter 3 tick angers.
    outcome = _rewrite_chapter_two(book, version=2, delta=_chapter_delta(
        book.project_id, 2, "delta-ch2-v2", _set(CAMP, "state.mood", "calm", "wary"),
    ))

    assert outcome.blocked is False, outcome.failure_reason
    assert book.tick_3 not in _delta_ids(book)
    assert book.tick_2 in _delta_ids(book)
    assert _state(book, 3, CAMP)["mood"] == "wary"
    assert _state(book, 3, GATE)["phase"] == "open"
    [conflict] = _conflicts(book)
    assert json.loads(conflict.proposed_change_json) == _replay_change(book.tick_3, "tick-3")
    assert (conflict.detected_after_chapter, conflict.story_time, conflict.blocks_from_chapter) == (3, 3060, 4)
    assert (conflict.impact_level, json.loads(conflict.involved_refs_json)) == ("minor", [CAMP])
    with Session() as session:
        commit = session.get(CanonCommitRecord, book.commit_ids[3])
        snapshot = session.get(WorldSnapshotRow, commit.world_snapshot_id)
    assert json.loads(snapshot.source_delta_ids_json) == ["delta-ch3"]


# the side effects of resolutions a dropped tick carried


def _wary_rewrite(book: _Book) -> CanonAdmissionOutcome:
    """A chapter 2 rewrite that already moves the camp the chapter 3 tick angers."""
    return _rewrite_chapter_two(book, version=2, delta=_chapter_delta(
        book.project_id, 2, "delta-ch2-v2", _set(CAMP, "state.mood", "calm", "wary"),
    ))


def test_dropping_a_tick_supersedes_its_resolutions_on_the_unwritten_plan(Session) -> None:
    book = _book(Session, tick_three_resolutions=("plot_responds", "accept_with_obligation"))
    responds, obliges = book.resolved[3]["plot_responds"], book.resolved[3]["accept_with_obligation"]
    _no_obligation, response_patch = _side_effects(book, responds)
    obligation_id, obligation_patch = _side_effects(book, obliges)
    status, goals, experience = _plan(book, 4)
    assert status == "planned"
    assert RESPONSE_GOAL in goals and any(obligation_id in goal for goal in goals)
    assert experience["obligations_to_resolve"] == [obligation_id]
    assert _obligation(book, obligation_id)[0] == "planned"

    outcome = _wary_rewrite(book)

    assert outcome.blocked is False, outcome.failure_reason
    assert book.tick_3 not in _delta_ids(book)
    # The world change the plot was to respond to is gone, and so is what it owed the plot.
    status, metadata = _obligation(book, obligation_id)
    assert status == "cancelled"
    assert book.tick_3 in metadata["cancel_reason"]
    status, goals, experience = _plan(book, 4)
    assert (status, goals) == ("planned", ["Commit chapter 4"])
    assert experience["obligations_to_resolve"] == []
    assert RESPONSE_GOAL not in experience["progress_markers"]
    assert not any(obligation_id in marker for marker in experience["progress_markers"])
    reverts = _revert_patches(book)
    assert sorted(json.loads(row.metadata_json)["reverted_plan_patch_id"] for row in reverts) == sorted(
        [response_patch, obligation_patch],
    )
    assert {(row.target_plan_id, row.target_scope, row.applied) for row in reverts} == {
        (book.chapter_ids[4], "chapter", True),
    }
    # The decided conflicts keep their record; the replay conflict lists what it superseded.
    with Session() as session:
        repository = WorldConflictRepository(session)
        assert {repository.get(book.project_id, conflict_id).status for conflict_id in (responds, obliges)} == {
            "resolved",
        }
    replay = _replay_conflict(book)
    change = json.loads(replay.proposed_change_json)
    assert change == _replay_change(
        book.tick_3, "tick-3",
        superseded_conflict_ids=change["superseded_conflict_ids"],
        cancelled_obligation_ids=[obligation_id],
        reverted_plan_patch_ids=change["reverted_plan_patch_ids"],
    )
    assert sorted(change["superseded_conflict_ids"]) == sorted([responds, obliges])
    assert sorted(change["reverted_plan_patch_ids"]) == sorted([response_patch, obligation_patch])
    assert replay.summary.endswith(
        " It supersedes 2 resolved conflict(s): 1 obligation(s) cancelled, 2 plan patch(es) reverted.",
    )
    with Session() as session:
        payload = json.loads(session.scalar(select(DecisionEvent.payload_json).where(
            DecisionEvent.related_object_id == replay.id,
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_DETECTED,
        )))
    assert payload["proposed_change"] == change


def test_dropping_a_tick_leaves_an_accepted_plan_alone(Session) -> None:
    # The chapter 2 tick's resolutions bound to chapter 3, which has since been written.
    book = _book(Session, tick_two_resolutions=("plot_responds", "accept_with_obligation"))
    responds, obliges = book.resolved[2]["plot_responds"], book.resolved[2]["accept_with_obligation"]
    obligation_id, _obligation_patch = _side_effects(book, obliges)
    before = _plan(book, 3)
    assert before[0] == "accepted"
    assert RESPONSE_GOAL in before[1]

    outcome = _sealing_rewrite(book, 2)

    assert outcome.blocked is False, outcome.failure_reason
    assert book.tick_2 not in _delta_ids(book)
    assert _plan(book, 3) == before
    assert _revert_patches(book) == []
    # The obligation was still open, so it goes with the change it answered.
    assert _obligation(book, obligation_id)[0] == "cancelled"
    replay = _replay_conflict(book)
    change = json.loads(replay.proposed_change_json)
    assert sorted(change["superseded_conflict_ids"]) == sorted([responds, obliges])
    assert (change["cancelled_obligation_ids"], change["reverted_plan_patch_ids"]) == ([obligation_id], [])
    assert replay.summary.endswith(
        " It supersedes 2 resolved conflict(s): 1 obligation(s) cancelled, 0 plan patch(es) reverted.",
    )


def test_an_aborted_rewrite_keeps_what_the_dropped_tick_resolved(Session) -> None:
    # Chapter 3 acts on the meeting the chapter 2 tick convened, so dropping that tick aborts the rewrite.
    book = _book(
        Session,
        chapter_three=(_set(MEETING, "state.phase", "convened", "adjourned"),),
        tick_two_resolutions=("accept_with_obligation",),
    )
    obliges = book.resolved[2]["accept_with_obligation"]
    obligation_id, _obligation_patch = _side_effects(book, obliges)

    outcome = _sealing_rewrite(book, 2)

    assert outcome.stale is True
    assert _obligation(book, obligation_id)[0] == "planned"
    assert [(conflict.id, conflict.status) for conflict in _conflicts(book)] == [(obliges, "resolved")]


@pytest.mark.parametrize(
    ("first_option", "both_dropped"),
    [("plot_responds", False), ("plot_responds", True), ("accept_with_obligation", False)],
    ids=["one_dropped", "both_dropped", "obligation_dropped"],
)
def test_a_shared_response_goal_stays_while_another_resolution_holds_it(
    Session, first_option: ConflictOption, both_dropped: bool,
) -> None:
    # The chapter 3 tick and a later direct resolve each answer a conflict on chapter 4 with the same
    # generic text: the plot_responds goal and marker, or the obligation's payoff markers.
    book = _book(Session, tick_three_resolutions=(first_option,))
    first = book.resolved[3][first_option]
    obligation_id, first_patch = _side_effects(book, first)
    direct_change = (
        _set(CAMP, "state.mood", "angry", "restless") if both_dropped else _create(book.project_id, "event-direct")
    )
    [direct] = _commit_tick(Session, _tick_delta(book.project_id, 3, "conflict-direct", direct_change)).graph_delta_ids
    second = _resolve_in_tick(Session, book.project_id, 3, direct, ("plot_responds",))["plot_responds"]
    _no_obligation, second_patch = _side_effects(book, second)
    _status, goals, experience = _plan(book, 4)
    assert goals.count(RESPONSE_GOAL) == 1 and experience["progress_markers"].count(RESPONSE_GOAL) == 1

    # The rewritten camp is wary: the tick's delta, which angered a calm camp, no longer applies.
    outcome = _wary_rewrite(book)

    assert outcome.blocked is False, outcome.failure_reason
    assert book.tick_3 not in _delta_ids(book)
    assert (direct in _delta_ids(book)) is not both_dropped
    reverted = sorted(json.loads(row.metadata_json)["reverted_plan_patch_id"] for row in _revert_patches(book))
    _status, goals, experience = _plan(book, 4)
    if both_dropped:
        assert reverted == sorted([first_patch, second_patch])
        assert (goals, experience["progress_markers"]) == (["Commit chapter 4"], [])
        return
    # The second resolution still holds the text it shares with the superseded one.
    assert reverted == [first_patch]
    assert goals == [RESPONSE_GOAL, "Commit chapter 4"]
    assert experience["progress_markers"] == [RESPONSE_GOAL]
    if first_option == "accept_with_obligation":
        # The obligation's own goal, contract entry and anchor go; nothing else holds them.
        assert _obligation(book, obligation_id)[0] == "cancelled"
        assert (experience["obligations_to_resolve"], experience["rule_anchors"]) == ([], [])
