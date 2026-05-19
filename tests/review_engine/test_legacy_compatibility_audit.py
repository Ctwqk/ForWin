from __future__ import annotations

import subprocess
import sys

from forwin.governance import DecisionEventInfo, DecisionEventType, ensure_decision_event_type
from forwin.orchestrator_loop_core.governance import _record_legacy_compatibility_event
from forwin.review_engine.audit import (
    LEGACY_COMPATIBILITY_REGISTRY,
    build_legacy_compatibility_payload,
    summarize_legacy_compatibility_audit,
)


def test_legacy_compatibility_event_type_is_registered() -> None:
    assert (
        ensure_decision_event_type(DecisionEventType.LEGACY_COMPATIBILITY_USED)
        == DecisionEventType.LEGACY_COMPATIBILITY_USED
    )


def test_legacy_compatibility_payload_records_facts_only() -> None:
    payload = build_legacy_compatibility_payload(
        compat_layer="book_state",
        compat_feature="book_state.state.location_fallback",
        usage_kind="read_fallback",
        source_module="forwin.book_state.runtime",
        usage_reason="state.location present",
        compat_key="state.location",
        legacy_identifier="旧城",
        canonical_identifier="",
        related_stage="compile_runtime",
        metadata={"field_path": "state.location"},
    )

    assert payload["compat_layer"] == "book_state"
    assert payload["compat_feature"] == "book_state.state.location_fallback"
    assert payload["usage_kind"] == "read_fallback"
    assert payload["source_module"] == "forwin.book_state.runtime"
    assert payload["usage_reason"] == "state.location present"
    assert payload["metadata"] == {"field_path": "state.location"}
    assert "delete_candidate" not in payload
    assert "blocking_for_removal" not in payload


def test_legacy_compatibility_summary_assesses_usage_after_collection() -> None:
    summary = summarize_legacy_compatibility_audit(
        [
            {
                "payload": build_legacy_compatibility_payload(
                    compat_layer="book_state",
                    compat_feature="book_state.state.location_fallback",
                    usage_kind="read_fallback",
                    source_module="forwin.book_state.runtime",
                    usage_reason="state.location present",
                )
            },
            {
                "payload": build_legacy_compatibility_payload(
                    compat_layer="projection",
                    compat_feature="projection.legacy_world_model_projection",
                    usage_kind="projection_compat",
                    source_module="forwin.orchestrator_loop_core.world_projection",
                    usage_reason="projection compatibility path invoked",
                )
            },
        ],
        registry=LEGACY_COMPATIBILITY_REGISTRY,
    )

    assert summary["total_events"] == 2
    assert summary["by_layer"]["book_state"] == 1
    assert summary["by_feature"]["book_state.state.location_fallback"] == 1
    blockers = summary["removal_assessment"]["blocking_for_removal"]
    assert {
        "compat_feature": "book_state.state.location_fallback",
        "reason": "used during audit window",
        "events": 1,
    } in blockers
    assert "delete_candidates" in summary["removal_assessment"]


def test_legacy_compatibility_summary_marks_unused_candidates() -> None:
    summary = summarize_legacy_compatibility_audit([], registry=LEGACY_COMPATIBILITY_REGISTRY)

    assert {
        "compat_feature": "governance.legacy_relaxed_fallback",
        "reason": "unused during audit window",
    } in summary["removal_assessment"]["delete_candidates"]


def test_record_legacy_compatibility_event_writes_fact_event() -> None:
    class Recorder:
        _governance_task_id = ""
        _governance_root_event_id = ""

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
    _record_legacy_compatibility_event(
        recorder,
        updater=object(),
        project_id="project-1",
        chapter_number=7,
        compat_layer="book_state",
        compat_feature="book_state.state.location_fallback",
        usage_kind="read_fallback",
        source_module="forwin.book_state.runtime",
        usage_reason="state.location present",
    )

    event = recorder.events[0]
    assert event.event_type == DecisionEventType.LEGACY_COMPATIBILITY_USED
    assert event.event_family == "runtime_observation"
    assert event.payload["compat_feature"] == "book_state.state.location_fallback"


def test_cutover_audit_help_exposes_legacy_compat_flag() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/audit_review_engine_cutover.py", "--help"],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "--include-legacy-compat" in result.stdout
