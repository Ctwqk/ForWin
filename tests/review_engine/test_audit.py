from __future__ import annotations

from forwin.governance import DecisionEventType, ensure_decision_event_type
from forwin.protocol.review import ReviewVerdict
from forwin.review_engine.audit import build_decision_event_payload, digest_decision_input
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth


def test_decision_event_payload_contains_rule_and_digest() -> None:
    payload = build_decision_event_payload(
        decision=Decision("manual_review", "needs human", "rule-1", ["deadline"], "router", {}),
        input_digest="abc123",
        shadow_mismatch=False,
    )

    assert payload["rule_id"] == "rule-1"
    assert payload["input_digest"] == "abc123"
    assert payload["missing_evidence"] == ["deadline"]


def test_decision_event_payload_records_live_shadow_sources() -> None:
    payload = build_decision_event_payload(
        decision=Decision("manual_review", "needs human", "rule-1", ["deadline"], "router", {}),
        input_digest="abc123",
        shadow_mismatch=True,
        live_or_shadow="live",
        legacy_outcome="manual_review",
        engine_outcome="auto_approve",
    )

    assert payload["live_or_shadow"] == "live"
    assert payload["legacy_outcome"] == "manual_review"
    assert payload["engine_outcome"] == "auto_approve"
    assert payload["shadow_mismatch"] is True


def test_review_engine_decision_event_type_is_registered() -> None:
    assert (
        ensure_decision_event_type(DecisionEventType.REVIEW_ENGINE_DECISION)
        == DecisionEventType.REVIEW_ENGINE_DECISION
    )


def test_policy_disabled_decision_event_explains_auto_approve_flag() -> None:
    payload = build_decision_event_payload(
        decision=Decision(
            "manual_review",
            "policy disabled: review_engine.auto_approve_enabled=false",
            "auto_approve_policy_disabled",
            [],
            "AutoDecisionEngine",
            {},
        ),
        input_digest="digest",
        shadow_mismatch=False,
    )

    assert payload["reason"] == "policy disabled: review_engine.auto_approve_enabled=false"
    assert payload["rule_id"] == "auto_approve_policy_disabled"


def test_decision_input_digest_is_stable_for_same_facts() -> None:
    input_payload = DecisionInput(
        project_id="project-1",
        chapter_number=1,
        review=ReviewVerdict(verdict="warn"),
        signals=[],
        open_obligations=[],
        operation_mode="copilot",
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=10,
        plan_layer_health=PlanLayerHealth(),
    )

    assert digest_decision_input(input_payload) == digest_decision_input(input_payload)
