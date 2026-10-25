import json

import pytest

from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.proposals import structured_patch
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet, FactNode, MapEdge, MapNode, NarrativeNode, WorldEdge, WorldNode,
)


@pytest.fixture
def runtime(monkeypatch):
    runtime = BookStateRuntime(project_id="p", as_of_chapter=2,
        world=ObjectiveWorldGraph(nodes=[WorldNode(id="actor", project_id="p", node_type="character")],
            states_by_node_id={"actor": {"status": "awake"}},
            facts=[FactNode(id="fact", project_id="p", proposition="test")],
            edges=[WorldEdge(id="edge", project_id="p", source_id="actor", target_id="actor",
                edge_type="ally_of", edge_family="social")]),
        map_graph=MapGraph(nodes=[MapNode(id="a", project_id="p", node_type="site")], edges=[MapEdge(id="road", project_id="p", from_node_id="a", to_node_id="b", edge_type="road")]))

    runtime.narrative.nodes_by_id["promise"] = NarrativeNode(
        id="promise", project_id="p", node_type="promise", status="open")

    def load(_self, project_id, *, as_of_chapter):
        assert (project_id, as_of_chapter) == ("p", 2)
        return runtime

    monkeypatch.setattr(structured_patch.BookStateProjection, "load_runtime_as_of", load)
    return runtime


def _delta(operation):
    row = KnowledgeEditProposalRow(id="edit", project_id="p", reason="edit",
        proposed_patch_json=json.dumps({"as_of_chapter": 0, "forwin_patch": [operation]}))
    return structured_patch.proposal_to_graph_delta(None, row, chapter_number=2)


@pytest.mark.parametrize("operation,kind,path,old", [
    ({"op": "set_node_field", "node_id": "actor", "field_path": "state.status", "new_value": "busy"}, "node_patches", "state.status", "awake"),
    ({"op": "append_manual_note", "node_id": "actor", "text": "note"}, "node_patches", "metadata", {}),
    ({"op": "set_node_field", "node_id": "actor", "field_path": "state.health", "new_value": "well"}, "node_patches", "state", {"status": "awake"}),
    ({"op": "deactivate_edge", "edge_id": "edge"}, "edge_patches", "is_active", True),
    ({"op": "deactivate_map_edge", "edge_id": "road"}, "map_patches", "status", "open"),
    ({"op": "set_edge_field", "edge_id": "edge", "field_path": "metadata.note", "new_value": "note"}, "edge_patches", "metadata", {}),
    ({"op": "set_map_node_field", "node_id": "a", "field_path": "metadata.note", "new_value": "note"}, "map_patches", "metadata", {}),
    ({"op": "set_fact_field", "fact_id": "fact", "field_path": "state.note", "new_value": "note"}, "fact_patches", "state", {}),
    ({"op": "set_reader_promise_field", "promise_id": "promise", "field_path": "current_debt_level", "new_value": 2}, "narrative_patches", "payload", {}),
])
def test_operator_patches_use_current_canon_values(runtime, operation, kind, path, old):
    delta = _delta(operation)
    patch = getattr(delta, kind)[0]
    assert (patch.field_path, patch.old_value) == (path, old)
    assert not BookStateCompiler(None).cas_mismatches(runtime, delta)
    verdict = BookStateReviewGate(None).review(ApprovedGraphDeltaSet(project_id="p", chapter_number=2,
        graph_deltas=[delta]), base_chapter=2, runtime=runtime)
    assert verdict.accepted, verdict.issues
    if operation["op"] == "append_manual_note":
        runtime.world.apply_delta(delta)
        assert runtime.world.get_node("actor").metadata == {"manual_notes": ["note"]}


@pytest.mark.parametrize("old", ["idle", None])
def test_supplied_old_value_is_preserved(runtime, old):
    delta = _delta({"op": "set_node_field", "node_id": "actor", "field_path": "state.status",
                    "old_value": old, "new_value": "busy"})
    assert delta.node_patches[0].old_value == old
    if old is not None:
        assert BookStateCompiler(None).cas_mismatches(runtime, delta)
    else:
        verdict = BookStateReviewGate(None).review(ApprovedGraphDeltaSet(project_id="p", chapter_number=2,
            graph_deltas=[delta]), base_chapter=2, runtime=runtime)
        assert not verdict.accepted


def test_seeded_knowledge_patch_compares_absent_fact():
    from forwin.book_state.knowledge import KnowledgeEvent, knowledge_event_to_patch
    patch = knowledge_event_to_patch(KnowledgeEvent(observer_type="reader", observer_id="reader",
        fact_ref="fact:f", state="knows", channel="seeded"), knowledge_by_ref={})
    assert patch.old_value == {"fact:f": None}
