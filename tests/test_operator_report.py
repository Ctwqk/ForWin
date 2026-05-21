from __future__ import annotations

from forwin.canon_quality.chapter_review_form.operator_report import build_report
from forwin.reviewer.repair_loop_detector import RepairAttemptRecord
from forwin.reviewer.repair_scope_router import RepairScopeKind, RoutedSignal


def _signal(kind: str, subject: str) -> RoutedSignal:
    return RoutedSignal(kind=kind, subject_key=subject, severity="error", description=kind)


def test_operator_report_contains_required_triage_fields() -> None:
    report = build_report(
        project_id="p1",
        chapter_number=18,
        latest_signals=[_signal("form_schema_invalid", "countdowns.0")],
        repair_history=[
            RepairAttemptRecord(
                attempt_no=1,
                scope=RepairScopeKind.OPERATOR.value,
                signals=[_signal("form_schema_invalid", "countdowns.0")],
                result_verdict="fail",
            )
        ],
        artifact_links={"form_artifact": "chapter_review_form/p1/18.json"},
    )

    assert report.project_id == "p1"
    assert report.chapter_number == 18
    assert report.latest_signals
    assert report.repair_history
    assert report.suspected_root_cause == "infrastructure"
    assert report.suggested_actions
    assert report.artifact_links["form_artifact"].endswith("18.json")
