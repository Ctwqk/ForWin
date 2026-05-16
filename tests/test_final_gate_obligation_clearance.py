from __future__ import annotations

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch


def test_final_gate_blocks_p1_obligation_even_with_valid_plan_patch() -> None:
    obligation = NarrativeObligation(
        id="obl-final",
        project_id="p1",
        origin_chapter_number=58,
        obligation_type="final_hook_closure",
        priority="P1",
        status="planned",
        summary="终章前仍需关闭主线 hook。",
        hardness="design_debt",
        deadline_chapter=60,
        payoff_test="终章必须关闭主线 hook。",
        linked_plan_patch_ids=["patch-final"],
    )
    patch = NarrativePlanPatch(
        id="patch-final",
        project_id="p1",
        target_scope="book",
        affected_chapters=[60],
        source_obligation_ids=["obl-final"],
        validation_status="passed",
        applied=True,
    )

    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=60,
        review_verdict="warn",
        obligations=[obligation],
        plan_patches=[patch],
        mode="strict",
        is_final_chapter=True,
    )

    assert result.commit_allowed is False
    assert result.admission_mode == "blocked"
    assert "final_obligation_not_cleared:obl-final" in result.blocking_reasons
