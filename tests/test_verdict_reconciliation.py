from __future__ import annotations

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.signals import CanonQualitySignal


def test_warning_only_deterministic_signals_do_not_block_strict_gate() -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=18,
        review_verdict="pass",
        signals=[
            CanonQualitySignal(
                signal_id="warn1",
                project_id="p1",
                chapter_number=18,
                signal_type="form_open_signal_persisting",
                severity="warning",
                description="还有叙事建议。",
            )
        ],
        mode="strict",
    )

    assert result.commit_allowed is True
    assert result.verdict == "warn"
    assert result.blocking_issue_count == 0


def test_llm_fail_verdict_blocks_even_without_deterministic_error_signal() -> None:
    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=18,
        review_verdict="fail",
        signals=[],
        mode="strict",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert "llm_review_fail" in result.blocking_reasons
