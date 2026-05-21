from __future__ import annotations

from forwin.review_engine.types import Decision, DecisionInput


def decide_auto_approve(
    *,
    input: DecisionInput,
    canon_gate_passed: bool,
    auto_approve_enabled: bool,
    future_plan_audit_passed: bool,
    obligation_audit_passed: bool,
    review_interval_hit: bool = False,
    chapters_since_last_full_review: int = 0,
    review_interval_chapters: int = 0,
) -> Decision:
    if not auto_approve_enabled:
        return Decision(
            outcome="manual_review",
            reason="policy disabled: review_engine.auto_approve_enabled=false",
            rule_id="auto_approve_policy_disabled",
            missing_evidence=[],
            routed_from="AutoDecisionEngine",
            sub_action={},
        )
    if (
        review_interval_hit
        and input.review.verdict in {"pass", "warn"}
        and canon_gate_passed
        and future_plan_audit_passed
        and obligation_audit_passed
        and not _has_error_signals(input)
        and not _has_blocking_obligations(input)
    ):
        return Decision(
            outcome="auto_approve",
            reason="interval-safe: canon gate, future plan audit, and obligation audit passed",
            rule_id="review_interval_safe",
            missing_evidence=[],
            routed_from="AutoDecisionEngine",
            sub_action={
                "review_interval_hit": True,
                "chapters_since_last_full_review": int(
                    chapters_since_last_full_review or 0
                ),
                "review_interval_chapters": int(review_interval_chapters or 0),
            },
        )
    if (
        input.operation_mode == "copilot"
        and input.review.verdict == "warn"
        and canon_gate_passed
        and future_plan_audit_passed
        and obligation_audit_passed
        and not _has_error_signals(input)
        and not _has_blocking_obligations(input)
    ):
        return Decision(
            outcome="auto_approve",
            reason="warn-only with passing gates",
            rule_id="copilot_safe_warn",
            missing_evidence=[],
            routed_from="AutoDecisionEngine",
            sub_action={"operation_mode": input.operation_mode},
        )
    return Decision(
        outcome="manual_review",
        reason="auto-approve conditions not met",
        rule_id="auto_approve_conditions_not_met",
        missing_evidence=["safe_warn_conditions"],
        routed_from="AutoDecisionEngine",
        sub_action={"operation_mode": input.operation_mode},
    )


def _has_error_signals(input: DecisionInput) -> bool:
    return any(str(signal.severity or "") == "error" for signal in input.signals)


def _has_blocking_obligations(input: DecisionInput) -> bool:
    for obligation in input.open_obligations:
        if obligation.status in {"blocked", "expired"}:
            return True
        if obligation.priority in {"P0", "P1"} and obligation.must_resolve_now:
            return True
    return False
