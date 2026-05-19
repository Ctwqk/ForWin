from __future__ import annotations

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review_engine.issue_taxonomy import classify_primary_issue, scope_for_issue_kind


def test_structural_identity_issue_routes_to_arc_plan() -> None:
    assert scope_for_issue_kind("identity_ambiguity") == "arc_plan"


def test_infrastructure_schema_issue_routes_to_operator() -> None:
    assert scope_for_issue_kind("form_schema_invalid") == "operator"


def test_unknown_issue_defaults_to_chapter_plan() -> None:
    assert scope_for_issue_kind("unknown_new_issue") == "chapter_plan"


def test_classifier_prefers_larger_scope_when_severity_is_comparable() -> None:
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="placeholder_leakage",
                severity="error",
                description="draft",
            ),
            ContinuityIssue(
                rule_name="identity_ambiguity",
                severity="error",
                description="arc",
            ),
        ],
    )

    primary = classify_primary_issue(review=review, signals=[])

    assert primary.kind == "identity_ambiguity"
    assert primary.scope == "arc_plan"


def test_classifier_uses_signal_kind_and_evidence_refs() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-1",
        project_id="project-1",
        chapter_number=3,
        signal_type="form_schema_invalid",
        severity="error",
        evidence_refs=["form:3"],
    )

    primary = classify_primary_issue(review=ReviewVerdict(verdict="warn"), signals=[signal])

    assert primary.kind == "form_schema_invalid"
    assert primary.scope == "operator"
    assert primary.evidence_refs == ("form:3",)
