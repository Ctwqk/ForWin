from __future__ import annotations

from dataclasses import replace

from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review_engine.engine import AutoDecisionEngine
from forwin.review_engine.rules.repair import build_scope_driven_repair_rules
from forwin.review_engine.rules.repair_v2 import decide_repair_v2
from forwin.review_engine.types import DecisionInput, PlanLayerHealth


def _input_with_issue(issue_kind: str, *, severity: str = "error") -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=8,
        review=ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name=issue_kind,
                    issue_type=issue_kind,
                    severity=severity,
                    description=issue_kind,
                    evidence_refs=[f"issue:{issue_kind}"],
                )
            ],
        ),
        signals=[],
        open_obligations=[],
        operation_mode="blackbox",
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=20,
        plan_layer_health=PlanLayerHealth(),
    )


def test_draft_level_issue_routes_to_local_repair() -> None:
    decision = decide_repair_v2(_input_with_issue("placeholder_leakage"))

    assert decision.outcome == "local_repair"
    assert decision.sub_action["scope"] == "draft"


def test_chapter_level_issue_routes_to_chapter_patch_scope() -> None:
    decision = decide_repair_v2(_input_with_issue("single_chapter_pacing"))

    assert decision.outcome == "chapter_patch"
    assert decision.sub_action["scope"] == "chapter_plan"


def test_band_level_issue_routes_to_band_patch_scope() -> None:
    decision = decide_repair_v2(_input_with_issue("identity_within_band"))

    assert decision.outcome == "band_patch"
    assert decision.sub_action["scope"] == "band_plan"


def test_arc_level_issue_routes_to_arc_patch_scope() -> None:
    decision = decide_repair_v2(_input_with_issue("identity_ambiguity"))

    assert decision.outcome == "arc_patch"
    assert decision.sub_action["scope"] == "arc_plan"


def test_book_level_issue_routes_to_book_patch_scope() -> None:
    decision = decide_repair_v2(_input_with_issue("book_structure_violation"))

    assert decision.outcome == "book_patch"
    assert decision.sub_action["scope"] == "book_plan"


def test_operator_issue_routes_to_manual_review_without_retry() -> None:
    decision = decide_repair_v2(_input_with_issue("form_schema_invalid"))

    assert decision.outcome == "manual_review"
    assert decision.sub_action["scope"] == "operator"
    assert decision.sub_action["max_attempts_for_scope"] == 0


def test_draft_issue_stays_draft_until_two_draft_attempts_are_spent() -> None:
    first_retry = _input_with_issue("body_truncated")
    second_retry = replace(_input_with_issue("body_truncated"), prior_scope_history=["draft"])

    assert decide_repair_v2(first_retry).sub_action["scope"] == "draft"
    assert decide_repair_v2(second_retry).sub_action["scope"] == "draft"


def test_draft_issue_escalates_to_chapter_plan_after_two_draft_attempts() -> None:
    input_payload = replace(
        _input_with_issue("body_truncated"),
        prior_scope_history=["draft", "draft"],
    )

    decision = decide_repair_v2(input_payload)

    assert decision.outcome == "chapter_patch"
    assert decision.sub_action["scope"] == "chapter_plan"
    assert decision.sub_action["escalated_from"] == "draft"


def test_arc_scope_escalates_after_one_arc_attempt() -> None:
    input_payload = replace(
        _input_with_issue("identity_ambiguity"),
        prior_scope_history=["arc_plan"],
    )

    decision = decide_repair_v2(input_payload)

    assert decision.outcome == "book_patch"
    assert decision.sub_action["scope"] == "book_plan"


def test_engine_uses_repair_v2_when_enabled() -> None:
    engine = AutoDecisionEngine(
        build_scope_driven_repair_rules(repair_v2_enabled=True)
    )

    decision = engine.decide(_input_with_issue("identity_ambiguity"))

    assert decision.rule_id == "repair_v2_scope_driven"
    assert decision.outcome == "arc_patch"


def test_scope_driven_repair_rules_always_use_repair_v2_after_safety_net_removal() -> None:
    engine = AutoDecisionEngine(
        build_scope_driven_repair_rules(repair_v2_enabled=False)
    )

    decision = engine.decide(_input_with_issue("identity_ambiguity"))

    assert decision.rule_id == "repair_v2_scope_driven"
    assert decision.outcome == "arc_patch"
    assert decision.routed_from == "RepairPolicy.v2"
