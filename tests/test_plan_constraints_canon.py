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


def _plan_gate(payload=None, *, patches=None, runtime=None):
    from forwin.book_state.reviewer import BookStateReviewGate
    from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NarrativePatch

    gate = BookStateReviewGate(None)
    gate.projection.load_runtime_as_of = lambda *a, **k: runtime or _runtime()
    if patches is None:
        patches = [NarrativePatch(target_ref="review_constraint:new", op="create", new_value={
            "id": "new", "project_id": "p", "node_type": "review_constraint", "payload": payload,
        })]
    delta = GraphDelta(id="plan", project_id="p", source_type="plan_constraint", narrative_patches=patches)
    return gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=0, graph_deltas=[delta]), base_chapter=0)


def test_gate_accepts_reader_must_not_know():
    verdict = _plan_gate({"ref": "reader", "kind": "must_not_know", "until_chapter": 3,
                          "observer_ref": "reader", "fact_ref": "fact:secret"})
    assert verdict.accepted, verdict.issues


@pytest.mark.parametrize("missing", ["observer_ref", "fact_ref"])
def test_gate_rejects_one_missing_knowledge_protection_ref(missing):
    payload = {"ref": "a", "kind": "must_not_know", "until_chapter": 3,
               "observer_ref": "a", "fact_ref": "fact:secret"}
    del payload[missing]
    verdict = _plan_gate(payload)
    assert not verdict.accepted
    assert {i.code for i in verdict.issues if i.severity == "error"} == {"invalid_protection"}


@pytest.mark.parametrize("status", [None, "unknown", "", 1, [], {}])
def test_gate_rejects_invalid_protection_status(status):
    verdict = _plan_gate({"ref": "a", "kind": "must_exist", "until_chapter": 3, "status": status})
    assert not verdict.accepted
    assert {i.code for i in verdict.issues if i.severity == "error"} == {"invalid_protection"}


@pytest.mark.parametrize("status", ["active", "released"])
def test_gate_accepts_valid_protection_status(status):
    assert _plan_gate({"ref": "a", "kind": "must_exist", "until_chapter": 3, "status": status}).accepted


def test_gate_releases_open_protection():
    from forwin.protocol.book_state import NarrativePatch

    protection = _protection("must_be_alive")
    protection.payload.pop("until_chapter", None)
    verdict = _plan_gate(runtime=_runtime(protections=[protection]), patches=[NarrativePatch(
        target_ref="review_constraint:protection", op="set", field_path="payload.status",
        old_value=None, new_value="released")])
    assert verdict.accepted, verdict.issues


@pytest.mark.parametrize("status,exists,expected", [(None, True, 1), ("active", True, 1),
                                                   ("released", True, 0), (None, False, 0)])
def test_plan_constraint_replay_rebases_absolute_releases(status, exists, expected):
    from forwin.canon.historical_rewrite import _rebase_plan_constraints
    from forwin.protocol.book_state import GraphDelta, NarrativePatch

    protection = _protection("must_exist", status=status)
    runtime = _runtime(protections=[protection] if exists else [])
    delta = GraphDelta(id="plan", project_id="p", source_type="plan_constraint", narrative_patches=[
        NarrativePatch(target_ref="review_constraint:protection", op="set", field_path="payload.status",
                       old_value="released", new_value="released"),
    ])
    rebased = _rebase_plan_constraints(runtime, delta, rewritten_chapter=2)
    assert len(rebased.narrative_patches) == expected
    if expected:
        assert rebased.narrative_patches[0].old_value == status
    assert delta.narrative_patches[0].old_value == "released"


@pytest.mark.parametrize("field,ref", [("ref", "gone"), ("observer_ref", "gone"), ("fact_ref", "fact:gone")])
def test_plan_constraint_replay_rejects_missing_create_refs(field, ref):
    from forwin.canon.historical_rewrite import HistoricalRewriteInvalid, _rebase_plan_constraints
    from forwin.protocol.book_state import GraphDelta, NarrativePatch

    payload = {"ref": "a", "kind": "must_not_know", "observer_ref": "a", "fact_ref": "fact:secret"}
    payload[field] = ref
    delta = GraphDelta(id="plan", project_id="p", source_type="plan_constraint", narrative_patches=[
        NarrativePatch(target_ref="review_constraint:new", op="create", new_value={
            "id": "new", "project_id": "p", "node_type": "review_constraint", "payload": payload}),
    ])
    with pytest.raises(HistoricalRewriteInvalid, match=f"rewriting chapter 2 removes {ref}, which beat-sheet protection new uses"):
        _rebase_plan_constraints(_runtime(), delta, rewritten_chapter=2)


def test_plan_constraint_replay_keeps_reader_create():
    from forwin.canon.historical_rewrite import _rebase_plan_constraints
    from forwin.protocol.book_state import GraphDelta, NarrativePatch

    delta = GraphDelta(id="plan", project_id="p", source_type="plan_constraint", narrative_patches=[
        NarrativePatch(target_ref="review_constraint:new", op="create", new_value={
            "id": "new", "project_id": "p", "node_type": "review_constraint", "payload": {
                "ref": "reader", "kind": "must_not_know", "observer_ref": "reader", "fact_ref": "fact:secret"}}),
    ])
    assert _rebase_plan_constraints(_runtime(), delta, rewritten_chapter=2) == delta


def test_constraint_commit_audits_created_and_released_ids_once(Session):
    from forwin.audit.events import DecisionEventType
    from forwin.models.audit import DecisionEvent

    world, _ = _book(Session)
    with Session.begin() as session:
        service = CanonAdmissionService()
        first = service.commit_plan_constraints(session=session, project_id=world.project_id, beat_sheet_id="audit-old",
            creates=[ProtectionSpec(ref=world.hero, kind="must_exist", until_chapter=8)], releases=[])
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        old = next(n.id for n in runtime.narrative.nodes_by_id.values() if n.metadata.get("beat_sheet_id") == "audit-old")
        args = dict(session=session, project_id=world.project_id, beat_sheet_id="audit-new",
                    creates=[ProtectionSpec(ref=world.hero, kind="must_be_alive", until_chapter=8)], releases=[old])
        second = service.commit_plan_constraints(**args)
        assert service.commit_plan_constraints(**args).idempotent
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        new = next(n.id for n in runtime.narrative.nodes_by_id.values() if n.metadata.get("beat_sheet_id") == "audit-new")
        events = list(session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == world.project_id, DecisionEvent.event_type == DecisionEventType.PLAN_CONSTRAINTS_COMMITTED)))
        payloads = {event.related_object_id: json.loads(event.payload_json) for event in events}
        assert len(events) == 2
        assert payloads == {
            "audit-old": {"beat_sheet_id": "audit-old", "graph_delta_ids": list(first.graph_delta_ids),
                          "created_ids": [old], "released_ids": [], "chapter_number": 0},
            "audit-new": {"beat_sheet_id": "audit-new", "graph_delta_ids": list(second.graph_delta_ids),
                          "created_ids": [new], "released_ids": [old], "chapter_number": 0},
        }


def test_revision_skips_previously_released_protections(Session):
    from forwin.book_state.repository import BookStateRepository

    world, arc_id = _book(Session)
    service = _service(StubLLM(_sheet(world), _sheet(world)))
    first = _ensure(Session, service, world, arc_id)
    with Session.begin() as session:
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        old_ids = sorted(n.id for n in runtime.narrative.nodes_by_id.values() if n.metadata.get("beat_sheet_id") == first)
        CanonAdmissionService().commit_plan_constraints(session=session, project_id=world.project_id,
            beat_sheet_id="already-released", creates=[], releases=[old_ids[0]])
        revised = service.revise(session, project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id),
                                 policy=POLICY, reason="revise", conflict_summary="change")
        delta = next(d for d in BookStateRepository(session).list_graph_deltas(world.project_id, through_chapter=0)
                     if d.metadata.get("beat_sheet_id") == revised.id)
        assert [p.target_ref for p in delta.narrative_patches if str(p.op) == "set"] == [f"review_constraint:{old_ids[1]}"]


def test_replay_routes_plan_constraints_without_tick_conflicts(monkeypatch):
    from types import SimpleNamespace
    from forwin.book_state.compiler import BookStateCompiler
    from forwin.canon.historical_rewrite import HistoricalCanonReplacement
    from forwin.protocol.book_state import GraphDelta, NarrativePatch

    runtime = _runtime(protections=[_protection("must_exist")])
    delta = GraphDelta(id="revision", project_id="p", source_type="plan_constraint", narrative_patches=[
        NarrativePatch(target_ref="review_constraint:protection", op="set", field_path="payload.status",
                       old_value="released", new_value="released"),
        NarrativePatch(target_ref="review_constraint:next", op="create", new_value={
            "id": "next", "project_id": "p", "node_type": "review_constraint",
            "payload": {"ref": "b", "kind": "must_exist", "until_chapter": 6}}),
    ])
    replacement = object.__new__(HistoricalCanonReplacement)
    replacement.plan = SimpleNamespace(chapter_number=2)
    replacement.vanished_refs = {}
    dropped = []
    monkeypatch.setattr(replacement, "_drop_world_tick_delta", lambda *args: dropped.append(args))
    compiler = BookStateCompiler(None)
    monkeypatch.setattr(compiler, "_persist_delta_side_effects", lambda *args: None)
    replayed = replacement._replay(runtime, [delta], compiler=compiler)
    assert [item.id for item in replayed] == [delta.id]
    assert runtime.narrative.nodes_by_id["protection"].payload["status"] == "released"
    assert "next" in runtime.narrative.nodes_by_id
    assert dropped == []


def test_amend_plan_release_ids_build_status_patches():
    from types import SimpleNamespace
    from forwin.world_engine.conflict_resolution import _release_candidate
    from forwin.world_engine.delta_builder import _TickBuilder
    from forwin.world_engine.types import TickContext
    from tests.test_world_constraints import CALENDAR, LEXICON

    runtime = _runtime(protections=[_protection("must_be_alive")])
    candidate = _release_candidate(SimpleNamespace(id="conflict", summary="release"),
                                   _candidate(_change("a", "alive_status", "dead")), runtime)
    assert candidate.protection_releases == ["protection"]
    result = _TickBuilder(context=TickContext(project_id="p", chapter_number=3, now=1000, window_start=900, seed=1),
                          tick_id="release", runtime=runtime, calendar=CALENDAR, lexicon=LEXICON).build([candidate], [])
    assert result.delta is not None, result.issues
    assert [(p.target_ref, p.field_path, p.old_value, p.new_value) for p in result.delta.narrative_patches] == [
        ("review_constraint:protection", "payload.status", None, "released"),
    ]
