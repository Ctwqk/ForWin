from __future__ import annotations

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.signals import CanonQualitySignal


def test_canon_admission_blocks_error_signal_in_strict_mode() -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=3,
        draft_id="d1",
        review_id="r1",
        review_verdict="warn",
        signals=[
            CanonQualitySignal(
                signal_id="sig-placeholder",
                project_id="p1",
                chapter_number=3,
                signal_type="placeholder_leakage",
                severity="error",
                target_scope="body",
                subject_key="placeholder:相关人员",
                description="正文包含占位符。",
                evidence_refs=["body:0-4"],
            )
        ],
        mode="strict",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.blocking_issue_count == 1
    assert result.deterministic_issue_refs == ["sig-placeholder"]


def test_canon_admission_shadow_records_but_allows_commit() -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=3,
        draft_id="d1",
        review_id="r1",
        review_verdict="warn",
        signals=[
            CanonQualitySignal(
                signal_id="sig-terminal",
                project_id="p1",
                chapter_number=3,
                signal_type="terminal_state_active_conflict",
                severity="error",
                target_scope="character",
                subject_key="character:韩砚",
                description="终止态后继续活跃。",
                evidence_refs=["body:10-20"],
            )
        ],
        mode="shadow",
    )

    assert result.commit_allowed is True
    assert result.verdict == "warn"
    assert result.blocking_issue_count == 1
    assert "shadow" in result.gate_summary


def test_canon_admission_fatal_only_blocks_form_invariant_drift_with_evidence() -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=26,
        draft_id="d1",
        review_id="r1",
        review_verdict="warn",
        signals=[
            CanonQualitySignal(
                signal_id="sig-invariant",
                project_id="p1",
                chapter_number=26,
                signal_type="form_invariant_drift",
                severity="error",
                target_scope="chapter",
                subject_key="city_renovation_deadline",
                description="deadline contradicted prior canon",
                evidence_refs=["quote:1"],
            )
        ],
        mode="fatal_only",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.deterministic_issue_refs == ["sig-invariant"]
    assert result.required_repair_scope == "chapter_plan"
