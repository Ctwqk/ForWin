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
