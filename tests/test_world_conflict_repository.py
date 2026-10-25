from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from forwin.models import Base, Project


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Project(id=project_id, title="Test", premise="Test", genre="test")
            for project_id in ("project-1", "project-2")
        ])
        session.commit()
        yield session
    engine.dispose()


def _draft(**overrides):
    from forwin.world_engine.conflict_types import ConflictDraft

    return ConflictDraft(**{
        "project_id": "project-1",
        "source": "world_tick",
        "detected_after_chapter": 3,
        "story_time": 3_000_000_000,
        "involved_refs": ["actor-1", "fact:1"],
        "violated": "protection",
        "impact_level": "major",
        "summary": "A protected state would change.",
        "proposed_change": {"patches": [{"op": "update", "field_path": "state.location_id", "old_value": "place-1", "new_value": "place-2"}]},
        "intent_ref": "intent-1",
        "actor_ref": "actor-1",
        "blocks_from_chapter": 4,
        **overrides,
    })


def _decision(option="veto_world"):
    from forwin.world_engine.conflict_types import ConflictDecision

    return ConflictDecision(
        option=option, decided_by="human", actor_id="reviewer-1",
        rationale="Preserve the constraint.", decided_at=datetime(2026, 10, 22, tzinfo=UTC),
    )


def test_create_list_and_open_blocking(session):
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    first = repo.create(_draft(blocks_from_chapter=5), required_decider="human", options=["veto_world", "amend_plan"])
    second = repo.create(_draft(), required_decider="human", options=["plot_responds"])
    third = repo.create(_draft(), required_decider="human", options=["veto_world"])
    first.created_at = datetime(2026, 10, 22)
    second.created_at = first.created_at + timedelta(seconds=1)
    third.created_at = first.created_at + timedelta(seconds=2)
    repo.create(_draft(project_id="project-2"), required_decider="human", options=["veto_world"])
    session.commit()
    session.expire_all()

    loaded = repo.get("project-1", first.id)
    assert loaded.status == "open"
    assert loaded.story_time == 3_000_000_000
    assert loaded.source == "world_tick"
    assert loaded.detected_after_chapter == 3
    assert loaded.violated == "protection"
    assert loaded.impact_level == "major"
    assert loaded.summary == "A protected state would change."
    assert loaded.intent_ref == "intent-1"
    assert loaded.actor_ref == "actor-1"
    assert loaded.updated_at is not None
    assert json.loads(loaded.involved_refs_json) == ["actor-1", "fact:1"]
    assert json.loads(loaded.proposed_change_json) == _draft().proposed_change
    assert json.loads(loaded.options_json) == ["veto_world", "amend_plan"]
    assert json.loads(loaded.decision_json) == {}
    assert json.loads(loaded.result_refs_json) == []
    assert [row.id for row in repo.list("project-1")] == [third.id, second.id, first.id]
    assert repo.get("project-2", first.id) is None
    assert repo.get("project-1", "missing") is None
    assert repo.get("project-1", first.id, for_update=True).id == first.id
    assert repo.open_blocking("project-1", next_chapter=5).id == second.id
    assert repo.open_blocking("project-1", next_chapter=3) is None
    assert repo.list("project-1", status="resolved") == []


def test_open_blocking_ignores_auto_codex_and_later_chapters(session):
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    for decider in ("auto", "codex"):
        repo.create(_draft(blocks_from_chapter=1), required_decider=decider, options=["veto_world"])
    later = repo.create(_draft(blocks_from_chapter=8), required_decider="human", options=["veto_world"])
    closed = repo.create(_draft(blocks_from_chapter=1), required_decider="human", options=["veto_world"])
    repo.mark_resolved(closed, decision=_decision(), result_refs=[])
    expired = repo.create(_draft(blocks_from_chapter=1), required_decider="human", options=["veto_world"])
    expired.status = "expired"
    repo.create(_draft(project_id="project-2", blocks_from_chapter=1), required_decider="human", options=["veto_world"])
    session.flush()

    assert repo.open_blocking("project-1", next_chapter=7) is None
    assert repo.open_blocking("project-1", next_chapter=8).id == later.id


def test_expire_only_open_rows_and_hides_from_blocking(session):
    from forwin.application.read_models.generation import (
        _derive_blocking_reason, load_open_world_conflicts, load_open_world_conflicts_by_project,
    )
    from forwin.world_engine.conflict_repository import ConflictNotOpen, WorldConflictRepository

    repo = WorldConflictRepository(session)
    row = repo.create(_draft(origin_key="tick-1"), required_decider="human", options=["veto_world"])
    assert repo.open_blocking("project-1", next_chapter=4).id == row.id
    grouped = load_open_world_conflicts_by_project(session, ["project-1"])
    for conflicts in (load_open_world_conflicts(session, "project-1"), grouped["project-1"]):
        assert [conflict.conflict_id for conflict in conflicts] == [row.id]
        assert _derive_blocking_reason(
            plans=[], latest_band_checkpoint=None, open_world_conflicts=conflicts, next_chapter=4,
        ).code == "world_conflict_pending"
    repo.expire(row, reason="chapter rewritten")
    session.commit()
    session.expire_all()
    assert row.status == "expired"
    assert row.origin_key == "tick-1"
    assert row.decision_option == ""
    assert row.decision_json == '{"reason":"chapter rewritten"}'
    assert repo.list("project-1", status="expired") == [row]
    assert repo.open_blocking("project-1", next_chapter=4) is None
    assert load_open_world_conflicts(session, "project-1") == []
    grouped = load_open_world_conflicts_by_project(session, ["project-1"])
    assert grouped == {"project-1": []}
    assert _derive_blocking_reason(
        plans=[], latest_band_checkpoint=None, open_world_conflicts=grouped["project-1"], next_chapter=4,
    ).code != "world_conflict_pending"
    with pytest.raises(ConflictNotOpen):
        repo.expire(row, reason="again")
    assert row.decision_option == ""
    assert row.decision_json == '{"reason":"chapter rewritten"}'
    resolved = repo.create(_draft(), required_decider="human", options=["veto_world"])
    repo.mark_resolved(resolved, decision=_decision(), result_refs=["delta-1"])
    decision_json = resolved.decision_json
    with pytest.raises(ConflictNotOpen):
        repo.expire(resolved, reason="closed")
    assert resolved.status == "resolved" and resolved.decision_json == decision_json


def test_origin_lookups_are_project_scoped_open_and_oldest_first(session):
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    rows = []
    for index, origin in enumerate(["tick-1", "tick-1", "tick-2"]):
        row = repo.create(_draft(origin_key=origin), required_decider="human", options=["veto_world"])
        row.created_at = datetime(2026, 10, 22) + timedelta(seconds=index)
        rows.append(row)
    repo.create(_draft(project_id="project-2", origin_key="tick-1"), required_decider="human", options=[])
    repo.create(_draft(origin_key=""), required_decider="human", options=[])
    assert repo.open_for_origin("project-1", "tick-1") == rows[0]
    assert repo.open_by_origin("project-1", ["tick-2", "tick-1", "tick-1"]) == rows
    assert repo.open_by_origin("project-1", []) == []
    assert repo.open_by_origin("project-1", [""]) == []
    assert repo.open_by_origin("project-1", ["", "tick-1", "tick-2"]) == rows
    assert repo.open_for_origin("project-1", "") is None
    assert repo.open_for_origin("project-1", "missing") is None
    repo.expire(rows[0], reason="retired")
    assert repo.open_for_origin("project-1", "tick-1") == rows[1]
    repo.mark_resolved(rows[1], decision=_decision(), result_refs=[])
    assert repo.open_by_origin("project-1", ["tick-1", "tick-2"]) == [rows[2]]
    assert repo.open_for_origin("project-1", "tick-1") is None


def test_pending_chapter_resumes_filters_and_orders_unconsumed_decisions(session):
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)

    def resolved(*, option="rewrite_chapter", refs=(), **overrides):
        row = repo.create(_draft(**{"source": "chapter_extraction", **overrides}),
                          required_decider="human", options=[option])
        repo.mark_resolved(row, decision=_decision(option), result_refs=refs)
        return row

    newer = resolved(option="amend_plan", refs=["plan_patch:1"])
    newer.created_at = datetime(2026, 10, 23)
    older = resolved()
    older.created_at = datetime(2026, 10, 22)
    resolved(refs=["plan_patch:2", "candidate:done"])
    resolved(project_id="project-2")
    resolved(blocks_from_chapter=5)
    resolved(source="world_tick")
    resolved(option="veto_world")
    closed = resolved()
    closed.status = "expired"
    repo.create(_draft(source="chapter_extraction"), required_decider="human", options=["rewrite_chapter"])
    assert repo.pending_chapter_resumes("project-1", chapter_number=4) == [older, newer]
    older.result_refs_json = '{"candidate:invalid_shape":true}'
    assert repo.pending_chapter_resumes("project-1", chapter_number=4) == [older, newer]
    repo.attach_result_refs(older, ["candidate:done"])
    assert repo.pending_chapter_resumes("project-1", chapter_number=4) == [newer]


def test_tick_and_slip_drafts_carry_tick_origin():
    from forwin.world_engine.intents import onstage_bookkeeping, select_due_intents
    from forwin.world_engine.tick_conflicts import review_draft
    from forwin.world_engine.types import TickCandidate, TickContext, WorldEffects
    from tests.test_world_tick_steps import NOW, _actor, _intent, _runtime

    runtime = _runtime(_actor("actor-1"), _intent("intent-1", "actor-1", at=NOW, onstage=True))
    context = TickContext(project_id=runtime.project_id, chapter_number=3, now=NOW, window_start=0, seed=1)
    held = review_draft(TickCandidate(
        candidate_id="candidate-1", origin="adjudication", actor_id="actor-1", intent_id="intent-1",
        summary="A major change.", effects=WorldEffects(), impact_level="major",
    ), context=context, runtime=runtime, tick_id="tick-1")
    assert held.draft.origin_key == "tick-1"
    result = onstage_bookkeeping(
        runtime, onstage_due=select_due_intents(runtime, now=NOW).onstage, already_due=[],
        interactions=[], now=NOW, max_slips=0, chapter_number=3, tick_id="tick-1",
        bound_intent_refs=frozenset({"intent-1"}), band_ended=False, band_bound_intent_refs=frozenset(),
    )
    assert [draft.origin_key for draft in result.conflicts] == ["tick-1"]


def test_conflict_readers_break_timestamp_ties_by_id(session):
    from forwin.application.read_models.generation import (
        _derive_blocking_reason, load_open_world_conflicts,
        load_open_world_conflicts_by_project,
    )
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    for conflict_id in ("conflict-z", "conflict-a"):
        row = repo.create(_draft(), required_decider="human", options=["veto_world"])
        row.id = conflict_id
        row.created_at = datetime(2026, 10, 22)
        session.flush()
    session.commit()
    assert repo.open_blocking("project-1", next_chapter=4).id == "conflict-a"
    assert [row.id for row in repo.list("project-1")] == ["conflict-a", "conflict-z"]
    batched = load_open_world_conflicts_by_project(session, ["project-1"])["project-1"]
    assert [row.conflict_id for row in batched] == ["conflict-a", "conflict-z"]
    for conflicts in (load_open_world_conflicts(session, "project-1"), list(reversed(batched))):
        reason = _derive_blocking_reason(
            plans=[], latest_band_checkpoint=None, open_world_conflicts=conflicts, next_chapter=4,
        )
        assert reason.code == "world_conflict_pending"
        assert reason.detail.startswith("conflict_id=conflict-a:")


def test_mark_resolved_twice_raises_conflict_not_open(session):
    from forwin.world_engine.conflict_repository import ConflictNotOpen, WorldConflictRepository

    repo = WorldConflictRepository(session)
    row = repo.create(_draft(), required_decider="human", options=["veto_world"])
    decision = _decision()
    repo.mark_resolved(row, decision=decision, result_refs=["delta-1"])
    session.commit()
    session.expire_all()
    loaded = repo.get("project-1", row.id)
    assert loaded.status == "resolved"
    assert json.loads(loaded.decision_json) == decision.model_dump(mode="json")
    assert json.loads(loaded.result_refs_json) == ["delta-1"]
    assert repo.open_blocking("project-1", next_chapter=4) is None
    assert [item.id for item in repo.list("project-1", status="resolved")] == [row.id]
    with pytest.raises(ConflictNotOpen):
        repo.mark_resolved(loaded, decision=_decision("amend_plan"), result_refs=[])
    assert json.loads(loaded.result_refs_json) == ["delta-1"]
    loaded.status = "expired"
    with pytest.raises(ConflictNotOpen):
        repo.mark_resolved(loaded, decision=decision, result_refs=[])


def test_reroll_count_counts_veto_decisions_per_actor(session):
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    for project, actor, option in [
        ("project-1", "actor-1", "veto_world"),
        ("project-1", "actor-1", "veto_world"),
        ("project-1", "actor-1", "plot_responds"),
        ("project-1", "actor-2", "veto_world"),
        ("project-2", "actor-1", "veto_world"),
    ]:
        row = repo.create(_draft(project_id=project, actor_ref=actor), required_decider="human", options=[option])
        repo.mark_resolved(row, decision=_decision(option), result_refs=[])
    repo.create(_draft(), required_decider="human", options=["veto_world"])
    assert repo.reroll_count("project-1", actor_ref="actor-1") == 2
    assert repo.reroll_count("project-1", actor_ref="actor-2") == 1
    assert repo.reroll_count("project-2", actor_ref="actor-1") == 1
    assert repo.reroll_count("project-1", actor_ref="missing") == 0
    # The default window starts at chapter 0: every veto the actor ever had counts.
    assert repo.reroll_count("project-1", actor_ref="actor-1", since_chapter=0) == 2


def test_reroll_count_counts_only_vetoes_detected_since_the_window_start(session):
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    for chapter in (1, 4, 5, 8):
        row = repo.create(_draft(detected_after_chapter=chapter), required_decider="human", options=["veto_world"])
        repo.mark_resolved(row, decision=_decision("veto_world"), result_refs=[])
    assert repo.reroll_count("project-1", actor_ref="actor-1") == 4
    assert repo.reroll_count("project-1", actor_ref="actor-1", since_chapter=5) == 2
    assert repo.reroll_count("project-1", actor_ref="actor-1", since_chapter=9) == 0


def test_escalate_persists_and_repository_leaves_commit_to_caller(session):
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    row = repo.create(_draft(), required_decider="codex", options=["veto_world"])
    row_id = row.id
    repo.escalate(row)
    session.commit()
    session.expire_all()
    assert repo.get("project-1", row_id).required_decider == "human"
    assert repo.open_blocking("project-1", next_chapter=4).id == row_id
    transient = repo.create(_draft(), required_decider="auto", options=["veto_world"])
    transient_id = transient.id
    session.rollback()
    assert repo.get("project-1", transient_id) is None


@pytest.mark.parametrize("field,value", [
    ("source", "unknown"), ("violated", "unknown"), ("impact_level", "unknown"),
])
def test_conflict_draft_rejects_unknown_tokens(field, value):
    with pytest.raises(ValidationError):
        _draft(**{field: value})


@pytest.mark.parametrize("impact_level", ["minor", "moderate", "major", "critical"])
def test_conflict_draft_accepts_impact_levels(impact_level):
    assert _draft(impact_level=impact_level).impact_level == impact_level


def test_conflict_decision_rejects_unknown_tokens():
    from forwin.world_engine.conflict_types import ConflictDecision

    payload = _decision().model_dump()
    for field in ("option", "decided_by"):
        with pytest.raises(ValidationError):
            ConflictDecision(**{**payload, field: "unknown"})


def test_typed_veto_filters_ignore_json_and_exclude_superseded(session):
    from forwin.world_engine.actor_turns import recent_vetoes
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    kept, retired = [repo.create(_draft(), required_decider="human", options=["veto_world"]) for _ in range(2)]
    for row in (kept, retired):
        repo.mark_resolved(row, decision=_decision(), result_refs=[])
    repo.mark_superseded(retired, graph_delta_id="delta-1")
    assert retired.superseded_by_delta == "delta-1"
    kept.decision_json = retired.decision_json = '{}'
    session.flush()
    assert repo.reroll_count("project-1", actor_ref="actor-1") == 1
    assert recent_vetoes(session, "project-1", actor_ids=["actor-1"])["actor-1"] == [kept]


def test_result_refs_are_written_atomically_and_match_exactly(session):
    from sqlalchemy import select
    from forwin.models.world_conflict import WorldConflictResultRefRow
    from forwin.world_engine.conflict_repository import WorldConflictRepository
    from forwin.world_engine.superseded_resolutions import supersede_resolutions

    repo = WorldConflictRepository(session)
    row = repo.create(_draft(), required_decider="human", options=["veto_world"])
    repo.mark_resolved(row, decision=_decision(), result_refs=['delta_%"', 'delta_%"'])
    repo.attach_result_refs(row, ['patch-1', 'patch-1'])
    assert set(session.scalars(select(WorldConflictResultRefRow.ref))) == {'delta_%"', 'patch-1'}
    assert json.loads(row.result_refs_json) == ['delta_%"', 'patch-1']
    assert supersede_resolutions(session, project_id="project-1", graph_delta_id='delta_', reason="rewrite").conflict_ids == ()
    assert supersede_resolutions(session, project_id="project-1", graph_delta_id='delta_%"', reason="rewrite").conflict_ids == (row.id,)
    session.rollback()
    assert list(session.scalars(select(WorldConflictResultRefRow.ref))) == []


def test_plot_response_lookups_use_typed_option(session):
    from forwin.world_engine.conflict_repository import WorldConflictRepository

    repo = WorldConflictRepository(session)
    row = repo.create(_draft(), required_decider="human", options=["plot_responds"])
    repo.mark_resolved(row, decision=_decision("plot_responds"), result_refs=[], pending_plan_response=True)
    assert row.decision_option == "plot_responds"
    row.decision_json = '{"pending_plan_response":true}'
    session.flush()
    assert repo.has_plot_response("project-1", "intent-1")
    assert repo.active_plot_responses("project-1") == [row]
    repo.clear_pending_plan_response(row)
    assert repo.active_plot_responses("project-1") == []


def test_delta_metadata_lookups_use_typed_ids(session):
    from forwin.book_state.repository import BookStateRepository
    from forwin.canon.admission import CanonAdmissionService, _world_tick_delta_ids
    from forwin.protocol.book_state import GraphDelta

    repo = BookStateRepository(session)
    tick = repo.append_graph_delta(GraphDelta(id="delta-tick", project_id="project-1", source_type="world_engine",
                                              metadata={"tick_id": 'tick_%"'}))
    plan = repo.append_graph_delta(GraphDelta(id="delta-plan", project_id="project-1", source_type="plan_constraint",
                                              metadata={"beat_sheet_id": 'sheet_%"'}))
    assert tick.tick_id == 'tick_%"' and plan.beat_sheet_id == 'sheet_%"'
    tick.metadata_json = plan.metadata_json = '{}'
    session.flush()
    assert _world_tick_delta_ids(session, "project-1", 'tick_%"') == ("delta-tick",)
    assert _world_tick_delta_ids(session, "project-1", 'tick_') == ()
    assert _world_tick_delta_ids(session, "project-2", 'tick_%"') == ()
    result = CanonAdmissionService().commit_plan_constraints(session=session, project_id="project-1",
                        constraint_set_id='sheet_%"', creates=[], releases=[])
    assert result.idempotent and result.graph_delta_ids == ("delta-plan",)
