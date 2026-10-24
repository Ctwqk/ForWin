from __future__ import annotations

import json

import pytest

from forwin.book_state import BookStateCompiler
from forwin.protocol.book_state import ApprovedGraphDeltaSet, FactPatch, GraphDelta, NodePatch, WorldNode
from tests.test_arc_beat_sheet import StubLLM
from tests.test_band_planner import Session, StubAudit, _band, _book, _feedback, _plan_band, _planner, _plans


@pytest.mark.parametrize("visibility_source", ["state", "profile"])
def test_band_payload_marks_concealed_actors_and_retries_named_hint(Session, visibility_source):
    world, arc_id, sheet_id = _book(Session)
    society = f"society-{world.project_id}"
    fact_id = f"society-fact-{world.project_id}"
    with Session.begin() as session:
        result = BookStateCompiler(session).compile(ApprovedGraphDeltaSet(
            project_id=world.project_id, chapter_number=0, graph_deltas=[GraphDelta(
                id=f"concealed-{world.project_id}", project_id=world.project_id, chapter_number=0,
                node_patches=[NodePatch(node_id=society, node_type="organization", op="create", new_value=WorldNode(
                    id=society, project_id=world.project_id, node_type="organization", name="HiddenSociety",
                    **{visibility_source: {"visibility_level": "secret"}},
                ).model_dump(mode="json"))],
                fact_patches=[FactPatch(fact_id=fact_id, op="create", proposition="society-truth",
                                        new_value={"project_id": world.project_id, "related_node_refs": [society]})],
            )],
        ))
        assert result.committed, result.blocked_reasons
    hint = {"hint_directives": [
        {"id": "hint-safe", "fact_ref": f"fact:{fact_id}", "hint_text": "an unexplained mark"},
        {"id": "hint-hidden", "fact_ref": f"fact:{fact_id}", "hint_text": "a trace of HiddenSociety"},
    ]}
    revealed = {**hint, "reveal_directives": [{
        "id": "reveal-society", "fact_ref": f"fact:{fact_id}", "observer": "reader", "target_state": "suspects",
    }]}
    client = StubLLM(_band(world, range(1, 5), {2: hint}), _band(world, range(1, 5), {2: revealed}))
    outcome = _plan_band(Session, _planner(client, StubAudit()), world, arc_id, sheet_id)

    assert outcome.band_plan_id is not None
    assert len(client.calls) == 2
    actors = {actor["id"]: actor for actor in client.payload(0)["actors"]}
    assert actors[society]["concealed"] is True
    assert actors[world.hero]["concealed"] is False
    assert "[writer_text_names_concealed] hint_directives[1].hint_text" in _feedback(client, 1)
    with Session() as session:
        assert json.loads(_plans(session, world.project_id)[2].hint_directives_json) == hint["hint_directives"]
