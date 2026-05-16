from __future__ import annotations

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch


def test_canon_admission_allows_planned_obligation_with_applied_patch() -> None:
    obligation = NarrativeObligation(
        id="obl-1",
        project_id="p1",
        origin_chapter_number=10,
        obligation_type="motivation_gap",
        priority="P1",
        status="planned",
        summary="韩砚动机尚未解释。",
        hardness="design_debt",
        deadline_chapter=11,
        payoff_test="第11章必须给出韩砚动机证据。",
        linked_plan_patch_ids=["patch-1"],
    )
    patch = NarrativePlanPatch(
        id="patch-1",
        project_id="p1",
        target_scope="chapter",
        affected_chapters=[11],
        source_obligation_ids=["obl-1"],
        validation_status="passed",
        applied=True,
    )

    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=10,
        draft_id="d10",
        review_id="r10",
        review_verdict="warn",
        mode="strict",
        obligations=[obligation],
        plan_patches=[patch],
    )

    assert result.commit_allowed is True
    assert result.verdict == "warn"
    assert result.admission_mode == "with_obligation"
    assert result.obligation_ids == ["obl-1"]
    assert result.required_plan_patch_ids == ["patch-1"]


def test_canon_admission_blocks_obligation_without_applied_plan_patch() -> None:
    obligation = NarrativeObligation(
        id="obl-1",
        project_id="p1",
        origin_chapter_number=10,
        obligation_type="motivation_gap",
        priority="P1",
        status="planned",
        summary="韩砚动机尚未解释。",
        hardness="design_debt",
        deadline_chapter=11,
        payoff_test="第11章必须给出韩砚动机证据。",
        linked_plan_patch_ids=["patch-1"],
    )

    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=10,
        draft_id="d10",
        review_id="r10",
        review_verdict="warn",
        mode="strict",
        obligations=[obligation],
        plan_patches=[],
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.admission_mode == "blocked"
    assert "missing_applied_plan_patch:patch-1" in result.blocking_reasons


def test_canon_admission_allows_active_obligation_before_deadline() -> None:
    obligation = NarrativeObligation(
        id="obl-active",
        project_id="p1",
        origin_chapter_number=10,
        obligation_type="motivation_gap",
        priority="P1",
        status="active",
        summary="韩砚动机尚未解释。",
        hardness="design_debt",
        deadline_chapter=12,
        payoff_test="第12章必须给出韩砚动机证据。",
        linked_plan_patch_ids=["patch-1"],
    )
    patch = NarrativePlanPatch(
        id="patch-1",
        project_id="p1",
        target_scope="chapter",
        affected_chapters=[12],
        source_obligation_ids=["obl-active"],
        validation_status="passed",
        applied=True,
    )

    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=11,
        review_verdict="pass",
        mode="strict",
        obligations=[obligation],
        plan_patches=[patch],
    )

    assert result.commit_allowed is True
    assert result.blocking_reasons == []


def test_canon_admission_blocks_active_obligation_at_deadline_when_unresolved() -> None:
    obligation = NarrativeObligation(
        id="obl-due",
        project_id="p1",
        origin_chapter_number=10,
        obligation_type="motivation_gap",
        priority="P1",
        status="active",
        summary="韩砚动机尚未解释。",
        hardness="design_debt",
        deadline_chapter=12,
        payoff_test="第12章必须给出韩砚动机证据。",
        linked_plan_patch_ids=["patch-1"],
    )
    patch = NarrativePlanPatch(
        id="patch-1",
        project_id="p1",
        target_scope="chapter",
        affected_chapters=[12],
        source_obligation_ids=["obl-due"],
        validation_status="passed",
        applied=True,
    )

    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=12,
        review_verdict="pass",
        mode="strict",
        obligations=[obligation],
        plan_patches=[patch],
    )

    assert result.commit_allowed is False
    assert "obligation_due_unresolved:obl-due" in result.blocking_reasons
