from __future__ import annotations

import json
from types import SimpleNamespace

from forwin.application.projects.reviews import _build_decision_layers
from forwin.audit.events import DecisionEventInfo, DecisionEventType
from forwin.naming import EntityAdmissionPlan, writer_output_admission_fingerprint
from forwin.protocol.book_state import MapEdge, MapNode
from forwin.protocol.context import ReviewContextPack
from forwin.protocol.writer import SceneOutput, TimeAdvance, WriterOutput


def _movement_context() -> ReviewContextPack:
    nodes = [
        MapNode(id="city", project_id="project-1", node_type="settlement", name="城"),
        MapNode(id="inner", project_id="project-1", node_type="site", name="内殿"),
    ]
    edge = MapEdge(
        id="long_road",
        project_id="project-1",
        from_node_id="city",
        to_node_id="inner",
        edge_type="road",
        bidirectional=True,
        travel_time=2.0,
    )
    return ReviewContextPack(
        project_id="project-1",
        project_title="测试书",
        chapter_number=1,
        chapter_plan_title="赶路",
        chapter_plan_one_line="主角抵达内殿。",
        map_context={
            "chapter_travel_time_budget": 0.25,
            "review_graph": {
                "available": True,
                "map_nodes": [node.model_dump(mode="json") for node in nodes],
                "map_edges": [edge.model_dump(mode="json")],
            },
        },
    )


def _movement_output() -> WriterOutput:
    output = WriterOutput(
        project_id="project-1",
        chapter_number=1,
        title="赶路",
        body="主角从城中抵达内殿。",
        end_of_chapter_summary="主角抵达内殿。",
        scene_outputs=[
            SceneOutput(
                scene_no=1,
                scene_objective="出发",
                scene_location_id="city",
                text="出发。",
            ),
            SceneOutput(
                scene_no=2,
                scene_objective="抵达",
                scene_location_id="inner",
                text="抵达。",
            ),
        ],
        time_advance=TimeAdvance(
            new_time_label="片刻后", duration_description="片刻后"
        ),
    )
    plan = EntityAdmissionPlan(
        project_id=output.project_id,
        chapter_number=output.chapter_number,
        candidate_fingerprint=writer_output_admission_fingerprint(output),
    )
    return output.model_copy(
        update={
            "generation_meta": {"entity_admission_plan": plan.model_dump(mode="json")}
        }
    )


def test_map_movement_reviewer_owns_deterministic_movement_issue() -> None:
    from forwin.review.map_movement import MapMovementReviewer

    verdict = MapMovementReviewer().review(_movement_context(), _movement_output())

    assert verdict.verdict == "fail"
    assert [issue.rule_name for issue in verdict.issues] == [
        "map_travel_time_exceeds_chapter_time"
    ]
    assert verdict.issues[0].reviewer == "map_movement"


def test_webnovel_reviewer_facade_keeps_legacy_movement_behavior_without_owning_method() -> (
    None
):
    from forwin.review.webnovel import WebNovelExperienceReviewer

    reviewer = WebNovelExperienceReviewer(llm_enabled=False)
    verdict = reviewer.review(_movement_context(), _movement_output())

    assert not hasattr(WebNovelExperienceReviewer, "_map_movement_issue")
    assert any(
        issue.rule_name == "map_travel_time_exceeds_chapter_time"
        for issue in verdict.issues
    )


def test_llm_webnovel_reviewer_owns_llm_prompt_and_json_repair() -> None:
    from forwin.review.llm_webnovel import LLMWebNovelReviewer
    from forwin.review.webnovel import WebNovelExperienceReviewer

    assert hasattr(LLMWebNovelReviewer, "_llm_review_messages")
    assert hasattr(LLMWebNovelReviewer, "_repair_llm_json")
    assert hasattr(LLMWebNovelReviewer, "_verdict_from_payload")
    assert not hasattr(WebNovelExperienceReviewer, "_review_with_llm")
    assert not hasattr(WebNovelExperienceReviewer, "_llm_payload")
    assert not hasattr(WebNovelExperienceReviewer, "_repair_llm_json")


def test_webnovel_reviewer_no_longer_carries_legacy_map_movement_helpers() -> None:
    from forwin.review.webnovel import WebNovelExperienceReviewer

    assert not hasattr(WebNovelExperienceReviewer, "_legacy_map_movement_issue")
    assert not hasattr(WebNovelExperienceReviewer, "_observer_cognition_views")
    assert not hasattr(WebNovelExperienceReviewer, "_map_path_issue")


def test_historical_draft_review_accepts_split_reviewer_ports() -> None:
    from forwin.protocol.review import ReviewVerdict
    from forwin.review.draft_service import DraftReviewService

    class Continuity:
        verdict = "pass"
        issues = []
        review_summary = ""

    class Checker:
        def check(self, project_id, writer_output):
            return Continuity()

    class StubReviewer:
        def __init__(self, verdict: str = "pass") -> None:
            self.verdict = verdict
            self.calls = 0

        def review(self, context, writer_output, **_kwargs):
            self.calls += 1
            return ReviewVerdict(verdict=self.verdict, issues=[])

    class StubLintCollector:
        def collect(self, writer_output):
            return []

    experience = StubReviewer()
    map_movement = StubReviewer("pass")
    plan_review = StubReviewer("pass")
    personality = StubReviewer("pass")
    hub = DraftReviewService(
        experience_reviewer=experience,
        map_movement_reviewer=map_movement,
        plan_reviewer=plan_review,
        personality_reviewer=personality,
        lint_collector=StubLintCollector(),
    )

    verdict = hub.review(
        project_id="project-1",
        context=_movement_context(),
        writer_output=_movement_output(),
        continuity_checker=Checker(),
    )

    assert verdict.verdict == "pass"
    assert experience.calls == 1
    assert map_movement.calls == 1
    assert plan_review.calls == 1
    assert personality.calls == 1


def test_historical_draft_review_merge_preserves_arc_repair_scope() -> None:
    from forwin.protocol.review import RepairInstruction
    from forwin.review.draft_service import DraftReviewService

    base = RepairInstruction(
        repair_scope="chapter_plan",
        failure_type="mixed",
        must_fix=["chapter pacing"],
        scope_reason="chapter-level issue",
    )
    arc = RepairInstruction(
        repair_scope="arc",
        failure_type="mixed",
        must_fix=["identity ambiguity"],
        scope_reason="arc-level issue",
    )

    merged = DraftReviewService._merge_repair_instructions(
        continuity_instruction=base,
        plan_instruction=None,
        webnovel_instruction=arc,
    )

    assert merged is not None
    assert merged.repair_scope == "arc"


def _layer_map(**overrides):
    values = {
        "review": SimpleNamespace(verdict="fail"),
        "review_meta": {},
        "issues": [],
        "rewrite_attempts": [],
        "candidate": None,
        "decision_refs": [],
    }
    values.update(overrides)
    return {
        layer.layer: layer
        for layer in _build_decision_layers(**values)
    }


def test_decision_layers_keep_repair_and_residual_eligibility_separate() -> None:
    attempt = SimpleNamespace(
        id="attempt-1",
        repair_scope="draft",
        result_verdict="pass",
        failure_reason="",
        verification_json=json.dumps(
            {
                "fixed_all_must_fix": True,
                "preserved_all_must_preserve": True,
            }
        ),
        source_draft_id="draft-1",
        result_draft_id="draft-2",
        result_review_id="review-2",
    )
    candidate = SimpleNamespace(
        id="candidate-2",
        status="ready_for_canon",
        canon_status="candidate",
        canon_commit_id="",
        canon_artifact_path="",
        failure_reason="",
        eligibility_decision_json=json.dumps(
            {
                "eligible": True,
                "body_hash": "body-hash",
                "plan_revision": "plan-v2",
            }
        ),
    )

    layers = _layer_map(rewrite_attempts=[attempt], candidate=candidate)

    assert layers["repair"].status == "complete"
    assert layers["repair"].outcome == "pass"
    assert layers["residual_eligibility"].outcome == "eligible"
    assert layers["canon"].status == "pending"


def test_hard_residual_decision_blocks_without_claiming_canon_failure() -> None:
    layers = _layer_map(
        review_meta={
            "final_residual_decision": {
                "decision": "manual_review_required",
                "forceable": False,
                "reason": "hard-residual-issue:canon_name_drift",
                "canon_risk": "high",
                "residual_issues": ["canon_name_drift"],
                "requires_human": True,
            }
        }
    )

    assert layers["residual_eligibility"].status == "blocked"
    assert layers["residual_eligibility"].blocking is True
    assert layers["canon"].outcome == "no_candidate"


def test_gate_delegation_distinguishes_no_delegate_from_spark_approval() -> None:
    no_delegate = _layer_map()["gate_delegation"]
    gate_events = [
        DecisionEventInfo(
            id="gate-approved",
            event_type=DecisionEventType.GATE_DELEGATION_APPROVED,
            summary="Spark gate approved",
            payload={"trace_id": "trace-1"},
            created_at="2026-07-10T02:00:00+00:00",
        ),
        DecisionEventInfo(
            id="gate-request",
            event_type=DecisionEventType.GATE_DELEGATION_REQUESTED,
            summary="Spark gate requested",
            payload={"trace_id": "trace-1"},
            created_at="2026-07-10T01:00:00+00:00",
        ),
    ]
    approved = _layer_map(decision_refs=gate_events)["gate_delegation"]

    assert no_delegate.status == "not_required"
    assert no_delegate.outcome == "not_delegated"
    assert approved.status == "complete"
    assert approved.outcome == "approved"
    assert [item.id for item in approved.decision_refs] == [
        "gate-request",
        "gate-approved",
    ]


def test_canon_layer_requires_candidate_commit_identity() -> None:
    committed_candidate = SimpleNamespace(
        id="candidate-1",
        status="accepted",
        canon_status="canon",
        canon_commit_id="commit-1",
        canon_artifact_path="canon/chapter-1.json",
        failure_reason="",
        eligibility_decision_json="{}",
    )
    blocked_candidate = SimpleNamespace(
        id="candidate-2",
        status="needs_review",
        canon_status="candidate",
        canon_commit_id="",
        canon_artifact_path="",
        failure_reason="canon quality hard block",
        eligibility_decision_json="{}",
    )

    committed = _layer_map(candidate=committed_candidate)["canon"]
    blocked = _layer_map(candidate=blocked_candidate)["canon"]

    assert committed.status == "complete"
    assert committed.outcome == "committed"
    assert committed.blocking is False
    assert blocked.status == "blocked"
    assert blocked.outcome == "needs_review"
    assert blocked.blocking is True
