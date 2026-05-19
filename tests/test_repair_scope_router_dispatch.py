from __future__ import annotations

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.reviewer.repair_scope_router import RepairScopeKind, route_review_repair_scopes, route_signal_kind


def test_schema_signal_routes_to_operator_and_not_draft() -> None:
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="schema",
                severity="error",
                description="ValidationError: Input should be a valid string",
                issue_type="form_schema_invalid",
            ),
            ContinuityIssue(
                rule_name="personality_drift",
                severity="error",
                description="动机偏移。",
                issue_type="personality_drift",
            ),
        ],
    )

    scopes = route_review_repair_scopes(review)

    assert scopes[0].scope == RepairScopeKind.OPERATOR
    assert {signal.kind for signal in scopes[0].signals} == {"form_schema_invalid"}


def test_subworld_then_draft_signals_keep_distinct_scopes() -> None:
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="subworld_admission_missing_canon_entity",
                severity="error",
                description="角色A未准入。",
                issue_type="subworld_admission_missing_canon_entity",
            ),
            ContinuityIssue(
                rule_name="personality_drift",
                severity="error",
                description="动机偏移。",
                issue_type="personality_drift",
            ),
        ],
    )

    scopes = route_review_repair_scopes(review)

    assert [scope.scope for scope in scopes] == [RepairScopeKind.SUBWORLD, RepairScopeKind.DRAFT]


def test_canon_quality_countdown_signal_routes_to_active_rules() -> None:
    signal = CanonQualitySignal(
        signal_id="s1",
        project_id="p1",
        chapter_number=18,
        signal_type="form_countdown_inconsistency",
        severity="error",
        subject_key="hidden_timer",
        description="隐藏倒计时状态漂移。",
    )

    scopes = route_review_repair_scopes(ReviewVerdict(verdict="fail"), signals=[signal])

    assert [scope.scope for scope in scopes] == [RepairScopeKind.ACTIVE_RULES]
    assert scopes[0].signals[0].source_signal_id == "s1"


def test_unknown_signal_kind_defaults_to_operator() -> None:
    assert route_signal_kind("new_unmapped_kind") == RepairScopeKind.OPERATOR
