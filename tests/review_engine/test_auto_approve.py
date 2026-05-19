from __future__ import annotations

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.review import ReviewVerdict
from forwin.review_engine.rules.auto_approve import decide_auto_approve
from forwin.review_engine.types import DecisionInput, PlanLayerHealth


def _input(
    *,
    verdict: str = "warn",
    mode: str = "copilot",
    signals: list[CanonQualitySignal] | None = None,
    obligations: list[NarrativeObligation] | None = None,
) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=12,
        review=ReviewVerdict(verdict=verdict),
        signals=list(signals or []),
        open_obligations=list(obligations or []),
        operation_mode=mode,
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=20,
        plan_layer_health=PlanLayerHealth(),
    )


def test_copilot_warn_only_auto_approves_when_flag_enabled() -> None:
    decision = decide_auto_approve(
        input=_input(),
        canon_gate_passed=True,
        auto_approve_enabled=True,
        future_plan_audit_passed=True,
        obligation_audit_passed=True,
    )

    assert decision.outcome == "auto_approve"
    assert decision.rule_id == "copilot_safe_warn"


def test_review_interval_safe_auto_approves_when_audits_pass() -> None:
    decision = decide_auto_approve(
        input=_input(verdict="pass", mode="blackbox"),
        canon_gate_passed=True,
        auto_approve_enabled=True,
        future_plan_audit_passed=True,
        obligation_audit_passed=True,
        review_interval_hit=True,
    )

    assert decision.outcome == "auto_approve"
    assert decision.rule_id == "review_interval_safe"
    assert "interval-safe" in decision.reason


def test_auto_approve_flag_off_returns_policy_disabled() -> None:
    decision = decide_auto_approve(
        input=_input(),
        canon_gate_passed=True,
        auto_approve_enabled=False,
        future_plan_audit_passed=True,
        obligation_audit_passed=True,
    )

    assert decision.outcome == "manual_review"
    assert decision.rule_id == "auto_approve_policy_disabled"


def test_auto_approve_rejects_error_signals() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-error",
        project_id="project-1",
        chapter_number=12,
        signal_type="placeholder_leakage",
        severity="error",
    )

    decision = decide_auto_approve(
        input=_input(signals=[signal]),
        canon_gate_passed=True,
        auto_approve_enabled=True,
        future_plan_audit_passed=True,
        obligation_audit_passed=True,
    )

    assert decision.outcome == "manual_review"
    assert "safe_warn_conditions" in decision.missing_evidence


def test_auto_approve_rejects_blocking_obligations() -> None:
    obligation = NarrativeObligation(
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="final_hook_closure",
        priority="P1",
        status="active",
        summary="主线义务未结清。",
        hardness="design_debt",
        deadline_chapter=12,
        payoff_test="必须结清主线义务。",
        must_resolve_now=True,
    )

    decision = decide_auto_approve(
        input=_input(obligations=[obligation]),
        canon_gate_passed=True,
        auto_approve_enabled=True,
        future_plan_audit_passed=True,
        obligation_audit_passed=True,
    )

    assert decision.outcome == "manual_review"
    assert "safe_warn_conditions" in decision.missing_evidence
