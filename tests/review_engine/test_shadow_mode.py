from __future__ import annotations

from forwin.review_engine.parity import compare_shadow_decisions, severe_shadow_mismatch
from forwin.review_engine.types import Decision


def _decision(outcome: str, rule_id: str = "rule") -> Decision:
    return Decision(
        outcome=outcome,  # type: ignore[arg-type]
        reason=outcome,
        rule_id=rule_id,
        missing_evidence=[],
        routed_from="test",
        sub_action={},
    )


def test_shadow_comparison_marks_mismatch() -> None:
    result = compare_shadow_decisions(
        live=Decision("manual_review", "old", "old_rule", [], "old", {}),
        shadow=Decision("system_block", "new", "new_rule", [], "engine", {}),
    )

    assert result.shadow_mismatch is True


def test_shadow_comparison_matches_same_outcome_and_action() -> None:
    result = compare_shadow_decisions(
        live=Decision("manual_review", "old", "old_rule", [], "old", {"review_action": "manual"}),
        shadow=Decision("manual_review", "new", "new_rule", [], "engine", {"review_action": "manual"}),
    )

    assert result.shadow_mismatch is False


def test_severe_mismatch_detects_fate_changing_outcome_difference() -> None:
    comparison = compare_shadow_decisions(
        live=_decision("manual_review"),
        shadow=_decision("auto_approve"),
    )

    assert comparison.shadow_mismatch is True
    assert severe_shadow_mismatch(comparison) is True


def test_severe_mismatch_ignores_same_outcome_payload_difference() -> None:
    comparison = compare_shadow_decisions(
        live=Decision("manual_review", "a", "rule", [], "test", {"reason": "a"}),
        shadow=Decision("manual_review", "b", "rule", [], "test", {"reason": "b"}),
    )

    assert comparison.shadow_mismatch is True
    assert severe_shadow_mismatch(comparison) is False
