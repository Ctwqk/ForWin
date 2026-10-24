import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from forwin.runtime.container import RuntimeContainer  # initialize the canon import graph
from forwin.book_state import reviewer
from forwin.book_state.knowledge import KNOWLEDGE_STATES
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.models.project import ChapterPlan
from forwin.planning.amend import AmendResult, PlanAmender
from forwin.planning.band_planner import service as band_service
from forwin.planning.band_planner.types import BandPlanningAuditError, PlanIssue
from forwin.planning.beat_sheet.types import BandSpan
from forwin.protocol.book_state import ApprovedGraphDeltaSet, FactNode, GraphDelta
from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.conflict_resolution import ConflictResolutionResult, ConflictResolutionService


POLICY = RuntimePolicy.for_profile("standard")


def _reveal_verdict(monkeypatch, targets, states, refs=("fact:secret",)):
    runtime = BookStateRuntime(
        project_id="p", as_of_chapter=0, map_graph=MapGraph(),
        world=ObjectiveWorldGraph(facts=[
            FactNode(id=ref.removeprefix("fact:"), project_id="p", proposition="Secret",
                     sensitivity_level="secret", truth_value="true") for ref in refs
        ]),
    )
    plan = ChapterPlan(reveal_directives_json=json.dumps([
        {"id": f"r-{index}", "fact_ref": "fact:secret", "observer": "reader", "target_state": target}
        for index, target in enumerate(targets)
    ]))
    gate = reviewer.BookStateReviewGate(SimpleNamespace(scalar=lambda _query: plan))
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(reviewer, "earliest_reveal_chapters", lambda *args: {
        ref.removeprefix("fact:"): 6 for ref in refs
    })
    delta = GraphDelta(id="delta", project_id="p", chapter_number=1, source_type="chapter_extraction", metadata={
        "knowledge_tags": [
            {"observer_type": "reader", "observer_id": "reader", "fact_ref": ref,
             "touches_secret_ref": ref, "state": state, "channel": "inferred"}
            for ref in refs for state in states
        ],
    })
    return gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=1, graph_deltas=[delta]), base_chapter=0)


@pytest.mark.parametrize("target", KNOWLEDGE_STATES)
@pytest.mark.parametrize("state", KNOWLEDGE_STATES)
def test_reader_directives_rank_only_progressive_knowledge(monkeypatch, target, state):
    ordered = ("heard", "suspects", "believes", "knows")
    allowed = state == target or (
        state in ordered and target in ordered and ordered.index(state) <= ordered.index(target)
    )
    verdict = _reveal_verdict(monkeypatch, [target], [state])
    assert verdict.accepted is allowed


def test_reader_directives_preserve_both_exact_and_ranked_permissions(monkeypatch):
    verdict = _reveal_verdict(monkeypatch, ["suspects", "misled"], ["heard", "suspects", "misled"])
    assert verdict.accepted
    assert not _reveal_verdict(monkeypatch, ["suspects", "misled"], ["knows"]).accepted


def test_premature_reveal_is_reported_once_per_ref(monkeypatch):
    verdict = _reveal_verdict(monkeypatch, [], ["heard", "knows", "misled", "knows"],
                              refs=("fact:secret", "fact:other"))
    assert [(issue.code, issue.target_ref) for issue in verdict.issues] == [
        ("premature_reveal", "fact:secret"), ("premature_reveal", "fact:other"),
    ]


@pytest.mark.parametrize("raises", [False, True])
def test_recording_an_amendment_failure_cannot_hide_the_durable_resolution(raises, caplog):
    session = Mock()
    issue = PlanIssue(code="audit_error", severity="error", message="audit failed")
    amender = Mock()
    if raises:
        amender.amend.side_effect = RuntimeError("audit failed")
    else:
        amender.amend.return_value = AmendResult(band_plan_id=None, issues=(issue,))
    amender.error_issue = PlanAmender.error_issue
    amender.record_world_amendment_failure.side_effect = RuntimeError("failure recorder unavailable")
    service = ConflictResolutionService(session, admission=object(), policy=POLICY, plan_amender=amender)
    result = ConflictResolutionResult(graph_delta_ids=("durable-delta",), obligation_id="obligation",
                                      plan_patch_id="patch", warnings=({"code": "existing", "message": "kept"},))

    returned = service._amend_after_world_change(SimpleNamespace(id="conflict"), object(), result)

    assert returned.graph_delta_ids == result.graph_delta_ids
    assert returned.obligation_id == result.obligation_id
    assert returned.plan_patch_id == result.plan_patch_id
    assert [warning["code"] for warning in returned.warnings] == ["existing", "replan_failed"]
    assert returned.followup_conflict_id == ""
    assert session.rollback.call_count == (2 if raises else 1)
    assert "failure recorder unavailable" in caplog.text


def test_a_failed_trace_preserves_the_original_band_audit_error(monkeypatch, caplog):
    cause = RuntimeError("original audit failure")
    issue = PlanIssue(code="audit_error", severity="error", message=str(cause))
    monkeypatch.setattr(band_service, "BookStateRepository", lambda _session: SimpleNamespace(
        latest_canon_chapter=lambda _project: 0,
    ))
    monkeypatch.setattr(band_service, "StoryCalendarRepository", lambda _session: SimpleNamespace(
        get=lambda _project: object(),
    ))
    monkeypatch.setattr(band_service, "ArcBeatSheetRepository", lambda _session: SimpleNamespace(load=lambda _row: None))
    monkeypatch.setattr(band_service, "BookStateProjection", lambda _session: SimpleNamespace(
        load_runtime_as_of=lambda *args, **kwargs: None,
    ))
    monkeypatch.setattr(band_service, "build_band_inputs", lambda *args, **kwargs: None)
    save_trace = Mock(side_effect=RuntimeError("transaction already aborted"))
    planner = band_service.BandPlanner(None, feasibility=object(), save_trace=save_trace)
    monkeypatch.setattr(planner, "_call", lambda *args, **kwargs: band_service._BandPlanCall(
        error=cause, failure_reason="audit_error", issues=[issue],
    ))
    with pytest.raises(BandPlanningAuditError, match="original audit failure") as raised:
        planner._plan(
            object(), project_id="p", arc_plan=SimpleNamespace(id="arc"), beat_sheet_row=object(),
            band=BandSpan(band_index=0, chapter_start=1, chapter_end=4), chapter_numbers=[1, 2, 3, 4],
            rows={}, policy=POLICY, notes=(), reason="plan_band", expected_band_plan_id=None,
        )
    assert raised.value.__cause__ is cause
    assert raised.value.issues == [issue]
    save_trace.assert_called_once()
    assert "transaction already aborted" in caplog.text
