from __future__ import annotations

import subprocess
import sys

from forwin.governance import DecisionEventInfo, DecisionEventType, ensure_decision_event_type
from forwin.orchestrator_loop_core.governance import _record_legacy_compatibility_event
from forwin.review_engine.audit import (
    LEGACY_COMPATIBILITY_REGISTRY,
    build_legacy_compatibility_payload,
    collect_legacy_compatibility_static_counts,
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
    assert any(
        item["compat_feature"] == "book_state.state.location_fallback"
        and item["reason"] == "used during audit window"
        and item["events"] == 1
        and item["static_callers"] is None
        and item["verdict"] == "repromote_mode"
        for item in blockers
    )
    assert "delete_candidates" in summary["removal_assessment"]


def test_legacy_compatibility_summary_marks_unused_candidates() -> None:
    summary = summarize_legacy_compatibility_audit([], registry=LEGACY_COMPATIBILITY_REGISTRY)

    assert any(
        item["compat_feature"] == "governance.legacy_relaxed_fallback"
        and item["reason"] == "unused during audit window"
        and item["events"] == 0
        and item["static_callers"] is None
        and item["verdict"] == "delete_candidate"
        for item in summary["removal_assessment"]["delete_candidates"]
    )


def test_uninstrumented_candidate_does_not_become_delete_candidate() -> None:
    registry = {
        "api.legacy_checkpoint_status": {
            "compat_layer": "api",
            "removal_mode": "candidate_if_unused",
            "instrumentation_status": "uninstrumented",
            "description": "Normalize legacy checkpoint status strings in API responses.",
        }
    }

    summary = summarize_legacy_compatibility_audit([], registry=registry)

    assert summary["removal_assessment"]["delete_candidates"] == []
    assert summary["removal_assessment"]["uninstrumented_no_delete_signal"] == [
        {
            "compat_feature": "api.legacy_checkpoint_status",
            "reason": "runtime instrumentation is missing",
            "events": 0,
            "static_callers": None,
            "verdict": "uninstrumented_no_delete_signal",
        }
    ]


def test_static_and_runtime_counts_drive_removal_verdicts() -> None:
    registry = {
        "compat.dead": {
            "compat_layer": "test",
            "removal_mode": "candidate_if_unused",
            "instrumentation_status": "instrumented",
        },
        "compat.rare": {
            "compat_layer": "test",
            "removal_mode": "candidate_if_unused",
            "instrumentation_status": "instrumented",
        },
        "compat.live": {
            "compat_layer": "test",
            "removal_mode": "candidate_if_unused",
            "instrumentation_status": "instrumented",
        },
        "compat.anomaly": {
            "compat_layer": "test",
            "removal_mode": "candidate_if_unused",
            "instrumentation_status": "instrumented",
        },
    }
    rows = [
        {
            "payload": build_legacy_compatibility_payload(
                compat_layer="test",
                compat_feature="compat.live",
                usage_kind="runtime",
                source_module="tests",
                usage_reason="live path",
            )
        },
        {
            "payload": build_legacy_compatibility_payload(
                compat_layer="test",
                compat_feature="compat.anomaly",
                usage_kind="runtime",
                source_module="tests",
                usage_reason="runtime without static caller",
            )
        },
    ]

    summary = summarize_legacy_compatibility_audit(
        rows,
        registry=registry,
        static_counts={
            "compat.dead": 0,
            "compat.rare": 2,
            "compat.live": 3,
            "compat.anomaly": 0,
        },
    )

    assessment = summary["removal_assessment"]
    assert assessment["delete_candidates"] == [
        {
            "compat_feature": "compat.dead",
            "reason": "no runtime events and no static callers",
            "events": 0,
            "static_callers": 0,
            "verdict": "delete_candidate",
        },
        {
            "compat_feature": "compat.rare",
            "reason": "static callers exist but runtime audit did not hit this path; verify in next run",
            "events": 0,
            "static_callers": 2,
            "verdict": "delete_candidate",
        }
    ]
    assert assessment["static_only_needs_targeted_test"] == []
    assert assessment["repromote_mode"] == [
        {
            "compat_feature": "compat.live",
            "reason": "used during audit window",
            "events": 1,
            "static_callers": 3,
            "verdict": "repromote_mode",
        }
    ]
    assert assessment["anomalous_runtime_without_static"] == [
        {
            "compat_feature": "compat.anomaly",
            "reason": "runtime events exist but static scan found no callers",
            "events": 1,
            "static_callers": 0,
            "verdict": "anomaly",
        }
    ]


def test_static_legacy_compatibility_counts_exclude_registry_and_tests(tmp_path) -> None:
    source_root = tmp_path / "forwin"
    (source_root / "review_engine").mkdir(parents=True)
    (source_root / "runtime.py").write_text(
        'record_compat(compat_feature="book_state.state.location_fallback")\n',
        encoding="utf-8",
    )
    (source_root / "review_engine" / "audit.py").write_text(
        '"book_state.state.location_fallback"\n',
        encoding="utf-8",
    )
    (source_root / "test_fake.py").write_text(
        '"book_state.state.location_fallback"\n',
        encoding="utf-8",
    )
    registry = {
        "book_state.state.location_fallback": {
            "compat_layer": "book_state",
            "static_patterns": ["book_state.state.location_fallback"],
        }
    }

    counts = collect_legacy_compatibility_static_counts(source_root, registry=registry)

    assert counts == {"book_state.state.location_fallback": 1}


def test_static_counts_ignore_imports_definitions_and_migrations(tmp_path) -> None:
    source_root = tmp_path / "forwin"
    (source_root / "book_state").mkdir(parents=True)
    (source_root / "migrations").mkdir(parents=True)
    (source_root / "api.py").write_text(
        "from forwin.book_state import LegacyBookStateImporter\n"
        "counts = LegacyBookStateImporter(session).import_project(project_id)\n",
        encoding="utf-8",
    )
    (source_root / "book_state" / "legacy_import.py").write_text(
        "class LegacyBookStateImporter:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (source_root / "migrations" / "001_legacy.py").write_text(
        "LegacyBookStateImporter(session).import_project(project_id)\n",
        encoding="utf-8",
    )
    registry = {
        "migration.legacy_book_state_import": {
            "compat_layer": "migration",
            "static_patterns": ["LegacyBookStateImporter("],
        }
    }

    counts = collect_legacy_compatibility_static_counts(source_root, registry=registry)

    assert counts == {"migration.legacy_book_state_import": 1}


def test_dead_code_candidate_with_no_runtime_or_static_is_reported() -> None:
    registry = {
        "dead_code.repair_loop_detector": {
            "compat_layer": "dead_code",
            "removal_mode": "dead_code_candidate",
            "instrumentation_status": "static_only",
        }
    }

    summary = summarize_legacy_compatibility_audit(
        [],
        registry=registry,
        static_counts={"dead_code.repair_loop_detector": 0},
    )

    assert summary["removal_assessment"]["dead_code"] == [
        {
            "compat_feature": "dead_code.repair_loop_detector",
            "reason": "no runtime events and no static callers",
            "events": 0,
            "static_callers": 0,
            "verdict": "dead_code",
        }
    ]


def test_legacy_compatibility_summary_includes_per_feature_detail() -> None:
    rows = [
        {
            "chapter_number": 3,
            "payload": build_legacy_compatibility_payload(
                compat_layer="subworld",
                compat_feature="subworld.legacy_entity_id_bridge",
                usage_kind="write_bridge",
                source_module="forwin.subworld_manager",
                usage_reason="legacy entity bridge",
                legacy_identifier="legacy-a",
                canonical_identifier="char-a",
            ),
        },
        {
            "chapter_number": 5,
            "payload": build_legacy_compatibility_payload(
                compat_layer="subworld",
                compat_feature="subworld.legacy_entity_id_bridge",
                usage_kind="write_bridge",
                source_module="forwin.subworld_manager",
                usage_reason="legacy entity bridge",
                legacy_identifier="legacy-a",
                canonical_identifier="char-a",
            ),
        },
    ]

    summary = summarize_legacy_compatibility_audit(
        rows,
        registry={
            "subworld.legacy_entity_id_bridge": {
                "compat_layer": "subworld",
                "removal_mode": "candidate_if_unused",
                "instrumentation_status": "instrumented",
            }
        },
        static_counts={"subworld.legacy_entity_id_bridge": 1},
    )

    detail = summary["per_feature_detail"]["subworld.legacy_entity_id_bridge"]
    assert detail["events"] == 2
    assert detail["unique_chapters"] == 2
    assert detail["events_per_chapter_avg"] == 1.0
    assert detail["top_legacy_identifiers"] == [["legacy-a", 2]]


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
