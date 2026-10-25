from copy import deepcopy
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.canon import CanonAdmissionService, CanonWriteFailure
from forwin.models import Base, Project
from forwin.models.book_state import WorldSnapshotRow
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.proposals.proposal_review import approve_world_edit_proposal
from forwin.proposals.structured_patch import proposal_to_graph_delta
from forwin.protocol.book_state import ApprovedGraphDeltaSet, FactNode, GraphDelta, NodePatch, WorldNode


def _runtime(state=None):
    runtime = BookStateRuntime(project_id="p", as_of_chapter=0,
        world=ObjectiveWorldGraph(nodes=[WorldNode(id="actor", project_id="p", node_type="character")]),
        map_graph=MapGraph())
    runtime.world.states_by_node_id["actor"] = state or {}
    return runtime


def _changes(delta):
    return ApprovedGraphDeltaSet(project_id="p", chapter_number=delta.chapter_number, graph_deltas=[delta])


def _proposal(proposal_id, operations):
    return KnowledgeEditProposalRow(id=proposal_id, project_id="p", source="manual", status="pending",
        target_page_key="actor", proposed_patch_json=json.dumps({"forwin_patch": operations}))


@pytest.mark.parametrize("state,path", [
    ({"health": "well"}, "state.health."),
    ({"health": "well"}, "state..health"),
    ({"health": "well"}, "state.health.detail"),
    ({"health": ["well"]}, "state.health.detail"),
    ({"health": []}, "state.health.detail"),
])
def test_first_state_write_cannot_hide_existing_value(state, path):
    runtime = _runtime(state)
    before = deepcopy(runtime.world.snapshot())
    delta = GraphDelta(id="d", project_id="p", chapter_number=1, node_patches=[
        NodePatch(node_id="actor", node_type="character", op="set", field_path=path, old_value=None, new_value="hurt")])
    verdict = BookStateReviewGate(None).review(_changes(delta), runtime=runtime, base_chapter=0)
    assert not verdict.accepted
    assert "missing_old_value" in {issue.code for issue in verdict.issues}
    assert runtime.world.snapshot() == before


@pytest.mark.parametrize("state", [{"health": "b"}, {"health": []}])
def test_explicit_null_cas_detects_populated_leaf_or_ancestor(state):
    delta = GraphDelta(id="d", project_id="p", chapter_number=1, node_patches=[
        NodePatch(node_id="actor", node_type="character", op="set", field_path="state.health.detail",
                  old_value=None, new_value="a")])
    assert BookStateCompiler(None).cas_mismatches(_runtime(state), delta)


def test_prepared_null_edit_cannot_overwrite_same_chapter_edit():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(Project(id="p", title="test", premise="test", genre="test"))
        session.flush()
        BookStateRepository(session).create_world_node(
            WorldNode(id="actor", project_id="p", node_type="character"), version_chapter=0, version_state={})
        session.add(WorldSnapshotRow(id="seed", project_id="p", as_of_chapter=0))
        operation = {"op": "set_node_field", "node_id": "actor", "field_path": "state.health", "old_value": None}
        a = _proposal("a", [{**operation, "new_value": "a"}])
        b = _proposal("b", [{**operation, "new_value": "b"}])
        session.add_all([a, b])
        session.flush()
        prepared = proposal_to_graph_delta(session, a, chapter_number=0)
        verdict = BookStateReviewGate(session).review(_changes(prepared))
        assert verdict.accepted, verdict.issues
        approve_world_edit_proposal(session, project_id="p", proposal_id="b")
        result = BookStateCompiler(session).compile(verdict.approved_changes)
        assert not result.committed
        assert result.cas_failed
        with pytest.raises(CanonWriteFailure):
            CanonAdmissionService().commit_world_edit(session=session, project_id="p", proposal_id="a",
                approved_changes=verdict.approved_changes, reason="test", trigger="test")
        assert a.status == "pending"
        assert b.status == "accepted"
        assert BookStateProjection(session).load_runtime_as_of("p", as_of_chapter=0).world.get_state("actor") == {"health": "b"}
        assert prepared.id not in {d.id for d in BookStateRepository(session).list_graph_deltas("p", through_chapter=0)}
    engine.dispose()


@pytest.mark.parametrize("operations,section,expected", [
    ([{"op": "append_manual_note", "text": text} for text in ("first", "second")],
     "metadata", {"manual_notes": ["first", "second"]}),
    ([{"op": "append_personality_note", "text": text} for text in ("first", "second")],
     "profile", {"personality_notes": ["first", "second"]}),
    ([{"op": "set_node_field", "field_path": "metadata.extra." + key, "new_value": value}
      for key, value in (("first", 1), ("second", 2))],
     "metadata", {"extra": {"first": 1, "second": 2}}),
])
def test_proposal_first_parent_merges_accumulate(monkeypatch, operations, section, expected):
    runtime = _runtime()
    before = deepcopy(runtime.world.snapshot())
    monkeypatch.setattr(BookStateProjection, "load_runtime_as_of", lambda *args, **kwargs: runtime)
    row = _proposal("notes", [{"node_id": "actor", **op} for op in operations])
    delta = proposal_to_graph_delta(None, row, chapter_number=0)
    assert all(p.old_value == {} for p in delta.node_patches)
    assert not BookStateCompiler(None).cas_mismatches(runtime, delta)
    verdict = BookStateReviewGate(None).review(_changes(delta), runtime=runtime, base_chapter=0)
    assert verdict.accepted, verdict.issues
    assert runtime.world.snapshot() == before
    BookStateProjection(None).apply_delta_to_runtime(runtime, delta)
    assert getattr(runtime.world.get_node("actor"), section) == expected


def test_tick_first_nested_state_write_builds_reviews_and_applies():
    from forwin.world_engine.delta_builder import _TickBuilder
    from forwin.world_engine.types import TickCandidate
    from tests.test_world_adjudicator import CALENDAR, CONTEXT, LEXICON, _runtime as tick_runtime

    runtime = tick_runtime()
    runtime.world.states_by_node_id["owner"] = {}
    candidate = TickCandidate(candidate_id="restraint", origin="adjudication", summary="change", effects={
        "entity_changes": [{"ref": "owner", "field": "restraint.state", "new_value": "imprisoned"}],
    })
    result = _TickBuilder(context=CONTEXT, tick_id="tick", runtime=runtime,
                          calendar=CALENDAR, lexicon=LEXICON).build([candidate], [])
    assert result.delta is not None, result.issues
    [patch] = result.delta.node_patches
    assert (patch.field_path, patch.op, patch.old_value) == ("state.restraint.state", "set", None)
    assert "old_value" in patch.model_fields_set
    verdict = BookStateReviewGate(None).review(_changes(result.delta), runtime=runtime, base_chapter=3)
    assert verdict.accepted, verdict.issues
    assert not BookStateCompiler(None).cas_mismatches(runtime, result.delta)
    BookStateProjection(None).apply_delta_to_runtime(runtime, result.delta)
    assert runtime.world.get_state("owner")["restraint"] == {"state": "imprisoned"}


@pytest.mark.parametrize("state", [{}, {"health": None}, {"restraint": None}])
def test_explicit_null_first_writes_pass_cas_before_and_after_serialization(state):
    runtime = _runtime(state)
    delta = GraphDelta(id="d", project_id="p", chapter_number=1, node_patches=[
        NodePatch(node_id="actor", node_type="character", op="set", field_path="state.health",
                  old_value=None, new_value="well"),
        NodePatch(node_id="actor", node_type="character", op="set", field_path="state.restraint.state",
                  old_value=None, new_value="free"),
    ])
    for candidate in (delta, GraphDelta.model_validate_json(delta.model_dump_json())):
        assert BookStateReviewGate(None).review(_changes(candidate), runtime=runtime, base_chapter=0).accepted
        assert not BookStateCompiler(None).cas_mismatches(runtime, candidate)
    BookStateProjection(None).apply_delta_to_runtime(runtime, delta)
    assert len(BookStateCompiler(None).cas_mismatches(runtime, delta)) == 2


def test_null_thread_appends_and_protection_release_keep_pre_delta_cas():
    from forwin.protocol.book_state import NarrativeNode, NarrativePatch

    runtime = _runtime()
    runtime.narrative.nodes_by_id.update({
        "thread": NarrativeNode(id="thread", project_id="p", node_type="plot_thread"),
        "guard": NarrativeNode(id="guard", project_id="p", node_type="review_constraint",
                               payload={"kind": "must_be_alive", "ref": "actor", "until_chapter": 0}),
    })
    delta = GraphDelta(id="d", project_id="p", chapter_number=1, narrative_patches=[
        *[NarrativePatch(target_ref="plot_thread:thread", op="append", field_path="payload.beats",
                         old_value=None, new_value={"description": text}) for text in ("first", "second")],
        NarrativePatch(target_ref="review_constraint:guard", op="set", field_path="payload.status",
                       old_value=None, new_value="released"),
    ])
    assert BookStateReviewGate(None).review(_changes(delta), runtime=runtime, base_chapter=0).accepted
    assert not BookStateCompiler(None).cas_mismatches(runtime, delta)
    BookStateProjection(None).apply_delta_to_runtime(runtime, delta)
    assert [beat["description"] for beat in runtime.narrative.nodes_by_id["thread"].payload["beats"]] == ["first", "second"]
    assert runtime.narrative.nodes_by_id["guard"].payload["status"] == "released"
    assert len(BookStateCompiler(None).cas_mismatches(runtime, delta)) == 3


@pytest.mark.parametrize("kind,op,path,value,old,status,accepted", [
    ("review_constraint", "set", "payload.status", "released", {"old_value": None}, {}, True),
    ("review_constraint", "replace", "payload.status", "released", {"old_value": None}, {"status": None}, True),
    ("plot_thread", "set", "payload.status", "released", {"old_value": None}, {}, False),
    ("review_constraint", "merge", "payload.status", "released", {"old_value": None}, {}, False),
    ("review_constraint", "set", "payload.other", "released", {"old_value": None}, {}, False),
    ("review_constraint", "set", "payload.status", "active", {"old_value": None}, {}, False),
    ("review_constraint", "set", "payload.status", "released", {}, {}, False),
    ("review_constraint", "set", "payload.status", "released", {"old_value": None}, {"status": "active"}, False),
])
def test_protection_null_gate_exception_stays_narrow(kind, op, path, value, old, status, accepted):
    from forwin.protocol.book_state import NarrativeNode, NarrativePatch

    runtime = _runtime()
    runtime.narrative.nodes_by_id["guard"] = NarrativeNode(id="guard", project_id="p", node_type=kind,
        payload={"kind": "must_be_alive", "ref": "actor", "until_chapter": 0, **status})
    delta = GraphDelta(id="d", project_id="p", chapter_number=1, narrative_patches=[
        NarrativePatch(target_ref="narrative_node:guard", op=op, field_path=path, new_value=value, **old)])
    verdict = BookStateReviewGate(None).review(_changes(delta), runtime=runtime, base_chapter=0)
    assert verdict.accepted is accepted, verdict.issues
    assert ("missing_old_value" in {issue.code for issue in verdict.issues}) is not accepted


@pytest.mark.parametrize("family", ["edge", "map_node", "fact"])
def test_proposal_parent_merge_accumulation_in_other_families(monkeypatch, family):
    from forwin.protocol.book_state import FactNode, MapNode, WorldEdge

    runtime = _runtime()
    if family == "edge":
        runtime.world.edges_by_id["target"] = WorldEdge(id="target", project_id="p", source_id="actor",
            target_id="actor", edge_type="trusts", edge_family="social")
        target = runtime.world.edges_by_id
        key = "edge_id"
    elif family == "map_node":
        runtime.map.add_node(MapNode(id="target", project_id="p", node_type="site"))
        target = runtime.map.nodes_by_id
        key = "node_id"
    else:
        runtime.world.facts_by_id["target"] = FactNode(id="target", project_id="p", proposition="test")
        target = runtime.world.facts_by_id
        key = "fact_id"
    monkeypatch.setattr(BookStateProjection, "load_runtime_as_of", lambda *args, **kwargs: runtime)
    row = _proposal("siblings", [{"op": f"set_{family}_field", key: "target",
        "field_path": "metadata.extra." + name, "new_value": value} for name, value in (("a", 1), ("b", 2))])
    delta = proposal_to_graph_delta(None, row, chapter_number=0)
    assert not BookStateCompiler(None).cas_mismatches(runtime, delta)
    assert BookStateReviewGate(None).review(_changes(delta), runtime=runtime, base_chapter=0).accepted
    BookStateProjection(None).apply_delta_to_runtime(runtime, delta)
    assert target["target"].metadata == {"extra": {"a": 1, "b": 2}}


@pytest.fixture
def proposal_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(Project(id="p", title="test", premise="test", genre="test"))
        session.flush()
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(id="actor", project_id="p", node_type="character"),
            version_chapter=0, version_state={})
        repo.create_fact_node(FactNode(id="f", project_id="p", proposition="test"))
        session.add(WorldSnapshotRow(id="seed", project_id="p", as_of_chapter=0))
        session.flush()
        yield session
    engine.dispose()


def test_explicit_null_old_value_cannot_rename_existing_node(proposal_session):
    session = proposal_session
    repo = BookStateRepository(session)
    repo.create_world_node(
        WorldNode(id="actor", project_id="p", node_type="character", name="Genesis name"),
        version_chapter=0,
    )
    delta = GraphDelta(id="rename", project_id="p", chapter_number=1, node_patches=[
        NodePatch(node_id="actor", node_type="character", op="set", field_path="name",
                  old_value=None, new_value="Retired name")])
    assert "old_value" in delta.node_patches[0].model_fields_set
    result = BookStateCompiler(session).compile(_changes(delta))
    assert not result.committed
    assert result.cas_failed
    assert repo.get_world_node("actor").name == "Genesis name"
    assert repo.list_graph_deltas("p", through_chapter=1) == []


@pytest.mark.parametrize("patch_op", ["set", "merge"])
@pytest.mark.parametrize("intervening_write", [False, True])
def test_first_knowledge_proposal_uses_absent_overlay_cas(proposal_session, patch_op, intervening_write):
    session = proposal_session
    runtime = BookStateProjection(session).load_runtime_as_of("p", as_of_chapter=0)
    assert ("character", "actor") not in runtime.cognition_by_observer
    operation = {"op": "set_cognition_field", "observer_type": "character", "observer_id": "actor",
                 "field_path": "knowledge_by_ref", "patch_op": patch_op}
    a = _proposal("a", [{**operation, "new_value": {"fact:f": "heard"}}])
    session.add(a)
    session.flush()
    prepared = proposal_to_graph_delta(session, a, chapter_number=0)
    [patch] = prepared.cognition_patches
    assert patch.old_value == ({"fact:f": None} if patch_op == "merge" else {})
    for supplied in (None, {"fact:f": "knows"}):
        explicit = proposal_to_graph_delta(session, _proposal("explicit", [
            {**operation, "old_value": supplied, "new_value": {"fact:f": "heard"}}]), chapter_number=0)
        assert explicit.cognition_patches[0].old_value == supplied
        assert BookStateCompiler(session).cas_mismatches(runtime, explicit)
    verdict = BookStateReviewGate(session).review(_changes(prepared))
    assert verdict.accepted, verdict.issues
    assert not BookStateCompiler(session).cas_mismatches(runtime, prepared)
    if intervening_write:
        b = _proposal("b", [{**operation, "new_value": {"fact:f": "believes"}}])
        session.add(b)
        session.flush()
        approve_world_edit_proposal(session, project_id="p", proposal_id="b")
        result = BookStateCompiler(session).compile(verdict.approved_changes)
        assert not result.committed
        assert result.cas_failed
        with pytest.raises(CanonWriteFailure, match="old_value mismatch"):
            CanonAdmissionService().commit_world_edit(session=session, project_id="p", proposal_id="a",
                approved_changes=verdict.approved_changes, reason="test", trigger="test")
        assert a.status == "pending"
        assert b.status == "accepted"
        assert prepared.id not in {d.id for d in BookStateRepository(session).list_graph_deltas("p", through_chapter=0)}
        expected = "believes"
    else:
        approve_world_edit_proposal(session, project_id="p", proposal_id="a")
        assert a.status == "accepted"
        expected = "heard"
    reloaded = BookStateProjection(session).load_runtime_as_of("p", as_of_chapter=0)
    assert reloaded.cognition_by_observer[("character", "actor")].knowledge_by_ref == {"fact:f": expected}


@pytest.mark.parametrize("ancestor", ["before", ["before"], []])
def test_supplied_ancestor_value_cannot_match_nested_leaf_cas(proposal_session, ancestor):
    session = proposal_session
    repo = BookStateRepository(session)
    repo.create_world_node(WorldNode(id="actor", project_id="p", node_type="character",
                                    metadata={"label": ancestor}), version_chapter=0, version_state={})
    before = BookStateProjection(session).load_runtime_as_of("p", as_of_chapter=0).world.get_node("actor").metadata
    proposal = _proposal("nested", [{"op": "set_node_field", "node_id": "actor",
        "field_path": "metadata.label.detail", "old_value": ancestor, "new_value": "after"}])
    session.add(proposal)
    session.flush()
    delta = proposal_to_graph_delta(session, proposal, chapter_number=1)
    assert delta.node_patches[0].old_value == ancestor
    verdict = BookStateReviewGate(session).review(_changes(delta), base_chapter=0)
    assert verdict.accepted, verdict.issues
    result = BookStateCompiler(session).compile(verdict.approved_changes)
    assert not result.committed
    assert result.cas_failed
    with pytest.raises(HTTPException) as exc:
        approve_world_edit_proposal(session, project_id="p", proposal_id="nested")
    assert exc.value.status_code == 409
    assert "old_value mismatch" in exc.value.detail
    assert proposal.status == "pending"
    assert repo.list_graph_deltas("p", through_chapter=1) == []
    repo.invalidate_project_range("p", from_chapter=1, through_chapter=1)
    assert next(node for node in repo.list_world_nodes("p") if node.id == "actor").metadata == before
    assert BookStateProjection(session).load_runtime_as_of("p", as_of_chapter=0).world.get_node("actor").metadata == before
