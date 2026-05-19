from __future__ import annotations

from forwin.narrative_obligations.types import NarrativePlanPatch
from forwin.planning.arc_patch_validator import ArcPatchValidator


def test_arc_patch_validator_requires_evidence_arc_and_payoff_test() -> None:
    patch = NarrativePlanPatch(project_id="project-1", target_scope="arc")

    result = ArcPatchValidator().validate(patch)

    assert result.passed is False
    assert "missing_source_evidence" in result.errors
    assert "missing_payoff_test" in result.errors
    assert "missing_target_arc_id" in result.errors


def test_arc_patch_validator_passes_evidenced_arc_patch() -> None:
    patch = NarrativePlanPatch(
        project_id="project-1",
        target_scope="arc",
        target_arc_id="arc-1",
        source_signal_ids=["sig-1"],
        expected_resolution_tests=["解释身份线索。"],
    )

    result = ArcPatchValidator().validate(patch)

    assert result.passed is True
    assert result.errors == []
