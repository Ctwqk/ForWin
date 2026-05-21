from __future__ import annotations

from forwin.governance import DecisionEventInfo, DecisionEventType, ensure_decision_event_type
from forwin.orchestrator_loop_core.governance import _record_engine_decision_event
from forwin.protocol.review import ReviewVerdict
from forwin.review_engine.audit import (
    build_decision_event_payload,
    digest_decision_input,
    summarize_live_cutover_audit,
)
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth
from scripts.audit_review_engine_cutover import cutover_audit_warnings


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
        live_source="engine",
        shadow_source="legacy",
        engine_live=True,
        legacy_shadow_evaluated=True,
        legacy_safety_net_used=False,
        severe_shadow_mismatch=True,
    )

    assert payload["live_or_shadow"] == "live"
    assert payload["legacy_outcome"] == "manual_review"
    assert payload["engine_outcome"] == "auto_approve"
    assert payload["shadow_mismatch"] is True
    assert payload["live_source"] == "engine"
    assert payload["shadow_source"] == "legacy"
    assert payload["engine_live"] is True
    assert payload["legacy_shadow_evaluated"] is True
    assert payload["legacy_safety_net_used"] is False
    assert payload["severe_shadow_mismatch"] is True


def test_live_cutover_audit_requires_engine_live_without_legacy_safety_net() -> None:
    summary = summarize_live_cutover_audit(
        [
            {
                "chapter_number": 1,
                "payload": {
                    "live_or_shadow": "live",
                    "live_source": "engine",
                    "engine_live": True,
                    "legacy_safety_net_used": False,
                    "severe_shadow_mismatch": False,
                },
            },
            {
                "chapter_number": 2,
                "payload": {
                    "live_or_shadow": "live",
                    "live_source": "legacy",
                    "engine_live": True,
                    "legacy_safety_net_used": True,
                    "severe_shadow_mismatch": False,
                },
            },
            {
                "chapter_number": 3,
                "payload": {
                    "live_or_shadow": "live",
                    "live_source": "engine",
                    "engine_live": True,
                    "legacy_safety_net_used": False,
                    "severe_shadow_mismatch": True,
                },
            },
        ],
        expected_chapters=3,
    )

    assert summary["passed"] is False
    assert summary["engine_live_chapters"] == 2
    assert summary["legacy_safety_net_chapters"] == [2]
    assert summary["severe_mismatch_chapters"] == [3]


def test_live_cutover_audit_passes_complete_60_chapter_engine_run() -> None:
    summary = summarize_live_cutover_audit(
        [
            {
                "chapter_number": chapter,
                "payload": {
                    "live_or_shadow": "live",
                    "live_source": "engine",
                    "engine_live": True,
                    "legacy_safety_net_used": False,
                    "severe_shadow_mismatch": False,
                },
            }
            for chapter in range(1, 61)
        ],
        expected_chapters=60,
    )

    assert summary["passed"] is True
    assert summary["observed_chapters"] == 60
    assert summary["missing_chapters"] == []


def test_cutover_audit_warns_when_engine_never_drove_live() -> None:
    warnings = cutover_audit_warnings(
        {
            "engine_live_chapters": 0,
            "observed_chapters": 60,
            "expected_chapters": 60,
        }
    )

    assert warnings == [
        "WARNING: ENGINE NEVER DROVE LIVE - Phase 1 pilot is not in allowlist or live flag is off; audit window not started"
    ]


def test_live_cutover_audit_aggregates_multiple_events_per_chapter() -> None:
    summary = summarize_live_cutover_audit(
        [
            {
                "chapter_number": 1,
                "payload": {
                    "live_or_shadow": "live",
                    "live_source": "engine",
                    "engine_live": True,
                    "legacy_safety_net_used": False,
                    "severe_shadow_mismatch": False,
                },
            },
            {
                "chapter_number": 1,
                "payload": {
                    "live_or_shadow": "shadow",
                    "live_source": "",
                    "engine_live": False,
                    "legacy_safety_net_used": False,
                    "severe_shadow_mismatch": False,
                },
            },
        ],
        expected_chapters=1,
    )

    assert summary["passed"] is True
    assert summary["engine_live_chapters"] == 1


def test_review_engine_decision_event_type_is_registered() -> None:
    assert (
        ensure_decision_event_type(DecisionEventType.REVIEW_ENGINE_DECISION)
        == DecisionEventType.REVIEW_ENGINE_DECISION
    )


def test_review_engine_decision_event_uses_valid_event_family() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.events: list[DecisionEventInfo] = []

        def _record_decision_event(self, **kwargs) -> None:
            self.events.append(
                DecisionEventInfo(
                    project_id=kwargs["project_id"],
                    chapter_number=kwargs["chapter_number"],
                    scope=kwargs["scope"],
                    event_family=kwargs["event_family"],
                    event_type=kwargs["event_type"],
                    summary=kwargs["summary"],
                    reason=kwargs["reason"],
                    payload=kwargs["payload"],
                )
            )

    recorder = Recorder()
    decision_input = DecisionInput(
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

    _record_engine_decision_event(
        recorder,
        updater=object(),
        decision=Decision("auto_approve", "ok", "rule-1", [], "router", {}),
        decision_input=decision_input,
    )

    assert recorder.events[0].event_family == "evaluation_verdict"
    assert recorder.events[0].event_type == DecisionEventType.REVIEW_ENGINE_DECISION


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
