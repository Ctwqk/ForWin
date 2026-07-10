from __future__ import annotations

from forwin.governance import (
    KNOWN_DECISION_EVENT_TYPES,
    DecisionEventType,
    ensure_decision_event_type,
)
from forwin.protocol.review import ReviewVerdict
from forwin.review.decision.audit import (
    build_decision_event_payload,
    digest_decision_input,
)
from forwin.review.decision.types import Decision, DecisionInput, PlanLayerHealth


def _input(*, chapter_number: int = 1) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=chapter_number,
        review=ReviewVerdict(verdict="pass"),
        signals=[],
        open_obligations=[],
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=10,
        plan_layer_health=PlanLayerHealth(),
    )


def test_decision_event_payload_contains_only_live_rule_contract() -> None:
    payload = build_decision_event_payload(
        decision=Decision(
            outcome="accept",
            reason="clean",
            rule_id="clean_accept",
            missing_evidence=[],
            routed_from="ReviewOutcomeRouter",
            sub_action={"review_action": "commit_clean"},
        ),
        input_digest="digest-1",
    )

    assert payload == {
        "rule_id": "clean_accept",
        "outcome": "accept",
        "reason": "clean",
        "missing_evidence": [],
        "routed_from": "ReviewOutcomeRouter",
        "sub_action": {"review_action": "commit_clean"},
        "input_digest": "digest-1",
    }


def test_decision_input_digest_is_stable_and_content_sensitive() -> None:
    assert digest_decision_input(_input()) == digest_decision_input(_input())
    assert digest_decision_input(_input()) != digest_decision_input(
        _input(chapter_number=2)
    )


def test_rule_decision_event_type_is_registered() -> None:
    assert DecisionEventType.RULE_DECISION_EVALUATED in KNOWN_DECISION_EVENT_TYPES
    assert (
        ensure_decision_event_type(DecisionEventType.RULE_DECISION_EVALUATED)
        == DecisionEventType.RULE_DECISION_EVALUATED
    )
