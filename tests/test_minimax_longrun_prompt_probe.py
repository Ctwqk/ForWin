from __future__ import annotations

from scripts.minimax_longrun_prompt_probe import ProbeAttempt, summarize_attempts


def test_summarize_attempts_counts_success_rate_by_case_and_model() -> None:
    attempts = [
        ProbeAttempt(
            case_name="history:scene_generation",
            model="MiniMax-M2.7",
            success=True,
            status_code=200,
            duration_ms=1200,
            output_chars=500,
        ),
        ProbeAttempt(
            case_name="history:scene_generation",
            model="MiniMax-M2.7",
            success=False,
            status_code=400,
            duration_ms=300,
            output_chars=0,
            error_category="bad_request",
        ),
        ProbeAttempt(
            case_name="noncode:summary",
            model="MiniMax-M2.7",
            success=True,
            status_code=200,
            duration_ms=900,
            output_chars=300,
        ),
    ]

    summary = summarize_attempts(attempts)

    assert summary["total_attempts"] == 3
    assert summary["successes"] == 2
    assert summary["success_rate"] == 2 / 3
    assert summary["by_case"]["history:scene_generation"]["success_rate"] == 0.5
    assert summary["by_case"]["noncode:summary"]["success_rate"] == 1.0
    assert summary["by_model"]["MiniMax-M2.7"]["failures"] == 1
    assert summary["error_categories"]["bad_request"] == 1
