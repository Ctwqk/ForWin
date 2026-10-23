from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from forwin.book_state.projection import BookStateProjection
from forwin.book_state.reveal_schedule import earliest_reveal_chapters
from forwin.canon.admission import CanonAdmissionService
from forwin.models.book_state import GraphDeltaRow
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import ArcPlanVersion
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import ArcBeatSheetData, ProtectionSpec
from forwin.world_engine.constraints import check_narrative_boundaries
from forwin.world_engine.tick_nodes import protected_refs
from tests.test_arc_beat_sheet import POLICY, Session, StubLLM, _book, _ensure, _service, _sheet
from tests.test_world_constraints import _candidate, _change, _protection, _runtime


def test_released_protection_is_ignored_by_all_readers():
    from forwin.world_engine.conflict_resolution import _broken_protections

    runtime = _runtime(protections=[_protection("must_be_alive", status="released", until_chapter=0)])
    held = _candidate(_change("a", "alive_status", "dead"))
    assert check_narrative_boundaries(runtime, held, next_chapter=1) == []
    assert protected_refs(runtime, next_chapter=1) == []
    assert _broken_protections(held, runtime) == []


def test_protected_refs_skips_must_not_know():
    runtime = _runtime(protections=[_protection("must_not_know", observer_ref="a", fact_ref="fact:secret")])
    assert protected_refs(runtime, next_chapter=4) == []
    assert check_narrative_boundaries(runtime, _candidate({"knowledge": [{
        "observer_ref": "a", "fact_ref": "fact:secret", "state": "knows", "channel": "witnessed",
    }]}), next_chapter=4)


def test_commit_plan_constraints_creates_protection_nodes_idempotently(Session):
    world, _ = _book(Session)
    with Session.begin() as session:
        service = CanonAdmissionService()
        kwargs = dict(session=session, project_id=world.project_id, beat_sheet_id="sheet-1",
                      creates=[ProtectionSpec(ref=world.hero, kind="must_be_alive", until_chapter=8)], releases=[])
        first = service.commit_plan_constraints(**kwargs)
        second = service.commit_plan_constraints(**kwargs)
        assert first.graph_delta_ids == second.graph_delta_ids
        assert not first.idempotent and second.idempotent
        rows = list(session.scalars(select(GraphDeltaRow).where(
            GraphDeltaRow.project_id == world.project_id, GraphDeltaRow.source_type == "plan_constraint")))
        assert len(rows) == 1 and rows[0].chapter_number == 0
        ordered = list(session.scalars(select(GraphDeltaRow).where(GraphDeltaRow.project_id == world.project_id)
                                       .order_by(GraphDeltaRow.created_at, GraphDeltaRow.id)))
        assert ordered[-1].id == first.graph_delta_ids[0]
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        violations = check_narrative_boundaries(runtime, _candidate(_change(world.hero, "alive_status", "dead")),
                                              next_chapter=1)
        assert len(violations) == 1
        assert runtime.narrative.nodes_by_id[violations[0].constraint_node_id].payload["source"] == "beat_sheet"


def test_revised_beat_sheet_releases_old_protections_in_same_delta(Session):
    world, arc_id = _book(Session)
    service = _service(StubLLM(_sheet(world), _sheet(world)))
    first = _ensure(Session, service, world, arc_id)
    with Session.begin() as session:
        before = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        old_ids = {n.id for n in before.narrative.nodes_by_id.values() if n.node_type == "review_constraint"}
        assert len(old_ids) == 2
        revised = service.revise(session, project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id),
                                 policy=POLICY, reason="revision", conflict_summary="change")
        assert revised.id != first
        row = session.scalars(select(GraphDeltaRow).where(
            GraphDeltaRow.project_id == world.project_id, GraphDeltaRow.source_type == "plan_constraint",
        ).order_by(GraphDeltaRow.created_at.desc(), GraphDeltaRow.id.desc())).first()
        from forwin.book_state.repository import BookStateRepository
        delta = next(d for d in BookStateRepository(session).list_graph_deltas(world.project_id, through_chapter=0)
                     if d.id == row.id)
        releases = [p for p in delta.narrative_patches if str(p.op) == "set"]
        assert {p.target_ref.split(":", 1)[1] for p in releases} == old_ids
        assert all((p.field_path, p.old_value, p.new_value) == ("payload.status", None, "released") for p in releases)
        assert len([p for p in delta.narrative_patches if str(p.op) == "create"]) == 2
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        assert all(runtime.narrative.nodes_by_id[ref].payload["status"] == "released" for ref in old_ids)
        violations = check_narrative_boundaries(runtime, _candidate(_change(world.hero, "alive_status", "dead")),
                                              next_chapter=1)
        assert len(violations) == 1 and violations[0].constraint_node_id not in old_ids


def test_reveal_schedule_prefers_beat_sheet_reader_targets(Session):
    world, arc_id = _book(Session)
    data = _sheet(world)
    data["reveal_targets"].append({"fact_ref": world.fact("fact-1"), "observer": world.hero,
                                   "target_state": "knows", "range": [2, 3]})
    _ensure(Session, _service(StubLLM(data)), world, arc_id)
    with Session() as session:
        assert earliest_reveal_chapters(session, world.project_id)[world.fact("fact-1").removeprefix("fact:")] == 9


def test_reveal_schedule_falls_back_to_live_arc_start_including_extended_arc(Session):
    world, arc_id = _book(Session)
    with Session.begin() as session:
        session.get(ArcPlanVersion, arc_id).chapter_start = 3
        session.add(ArcPlanVersion(project_id=world.project_id, arc_number=2, chapter_start=13, chapter_end=24, arc_synopsis="extended"))
        revision = session.scalars(select(BookGenesisRevision).where(BookGenesisRevision.project_id == world.project_id)).one()
        pack = json.loads(revision.pack_json)
        pack["world_seed"]["reveal_candidates"].append({"fact_id": "extended", "arc_number": 2})
        revision.pack_json = json.dumps(pack)
    with Session() as session:
        schedule = earliest_reveal_chapters(session, world.project_id)
        assert schedule[world.fact("fact-1").removeprefix("fact:")] == 3
        assert schedule[world.fact("extended").removeprefix("fact:")] == 13


def test_milestone_refs_include_unexpired_hard_milestones_and_current_exit_state(Session):
    from forwin.planning.beat_sheet.service import milestone_refs_for_chapter

    world, arc_id = _book(Session)
    data = _sheet(world)
    data["milestones"].append({"id": "soft", "description": "soft", "target_range": [9, 12],
                                "required_refs": ["soft-ref"], "hard": False})
    with Session.begin() as session:
        ArcBeatSheetRepository(session).create(world.project_id, arc_id, ArcBeatSheetData.model_validate(data),
                                               source="llm", reason="test")
        assert milestone_refs_for_chapter(session, world.project_id, 8) == frozenset({world.hero, world.rival})
        assert milestone_refs_for_chapter(session, world.project_id, 9) == frozenset({world.rival})
        assert milestone_refs_for_chapter(session, world.project_id, 13) == frozenset()


def test_protection_release_compares_absent_status_during_replay():
    from forwin.book_state.compiler import BookStateCompiler
    from forwin.protocol.book_state import GraphDelta, NarrativePatch

    runtime = _runtime(protections=[_protection("must_be_alive")])
    patch = NarrativePatch(target_ref="review_constraint:protection", op="set", field_path="payload.status",
                           old_value=None, new_value="released")
    delta = GraphDelta(id="release", project_id="p", source_type="plan_constraint", narrative_patches=[patch])
    compiler = BookStateCompiler(None)
    assert compiler.cas_mismatches(runtime, delta) == []
    runtime.narrative.apply_narrative_patch(patch, project_id="p")
    assert check_narrative_boundaries(runtime, _candidate(_change("a", "alive_status", "dead")), next_chapter=4) == []
    assert [label for label, _, _ in compiler.cas_mismatches(runtime, delta)] == [
        "narrative:review_constraint:protection:payload.status",
    ]
    runtime.narrative.nodes_by_id.clear()
    assert len(compiler.cas_mismatches(runtime, delta)) == 1


def test_constraints_follow_seed_and_prior_sheet_in_one_transaction(Session):
    from forwin.book_state.repository import BookStateRepository

    world, _ = _book(Session)
    with Session.begin() as session:
        service = CanonAdmissionService()
        first = service.commit_plan_constraints(session=session, project_id=world.project_id, beat_sheet_id="same-tx-1",
            creates=[ProtectionSpec(ref=world.hero, kind="must_be_alive", until_chapter=8)], releases=[])
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        protection = next(n for n in runtime.narrative.nodes_by_id.values() if n.metadata.get("beat_sheet_id") == "same-tx-1")
        second = service.commit_plan_constraints(session=session, project_id=world.project_id, beat_sheet_id="same-tx-2",
                                                creates=[], releases=[protection.id])
        deltas = BookStateRepository(session).list_graph_deltas(world.project_id, through_chapter=0)
        assert tuple(d.id for d in deltas[-2:]) == first.graph_delta_ids + second.graph_delta_ids
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        assert runtime.narrative.nodes_by_id[protection.id].payload["status"] == "released"
        assert check_narrative_boundaries(runtime, _candidate(_change(world.hero, "alive_status", "dead")),
                                          next_chapter=1) == []
