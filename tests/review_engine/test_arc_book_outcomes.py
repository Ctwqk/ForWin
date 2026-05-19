from __future__ import annotations

from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review_engine.rules.structural_patch import decide_structural_patch
from forwin.review_engine.types import DecisionInput, PlanLayerHealth


def _input_with_issue(issue_kind: str) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=10,
        review=ReviewVerdict(
            verdict="warn",
            issues=[
                ContinuityIssue(
                    rule_name=issue_kind,
                    issue_type=issue_kind,
                    severity="warning",
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


def test_arc_patch_disabled_routes_to_explicit_manual_review() -> None:
    decision = decide_structural_patch(
        input=_input_with_issue("identity_ambiguity"),
        arc_patcher_enabled=False,
        book_patcher_enabled=False,
    )

    assert decision.outcome == "manual_review"
    assert decision.rule_id == "arc_patcher_disabled"
    assert decision.missing_evidence == ["arc_patcher_enabled"]


def test_arc_patch_enabled_returns_arc_patch() -> None:
    decision = decide_structural_patch(
        input=_input_with_issue("identity_ambiguity"),
        arc_patcher_enabled=True,
        book_patcher_enabled=False,
    )

    assert decision.outcome == "arc_patch"
    assert decision.sub_action["patch_type"] == "arc_defer_acceptance"


def test_book_patch_disabled_routes_to_explicit_manual_review() -> None:
    decision = decide_structural_patch(
        input=_input_with_issue("book_structure_violation"),
        arc_patcher_enabled=True,
        book_patcher_enabled=False,
    )

    assert decision.outcome == "manual_review"
    assert decision.rule_id == "book_patcher_disabled"
    assert decision.missing_evidence == ["book_patcher_enabled"]


def test_book_patch_enabled_returns_book_patch() -> None:
    decision = decide_structural_patch(
        input=_input_with_issue("book_structure_violation"),
        arc_patcher_enabled=False,
        book_patcher_enabled=True,
    )

    assert decision.outcome == "book_patch"
    assert decision.sub_action["patch_type"] == "book_defer_acceptance"
