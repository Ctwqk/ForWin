from __future__ import annotations

from forwin.narrative_obligations.types import NarrativePlanPatch
from forwin.planning.book_patch_validator import BookPatchValidator


def test_book_patch_validator_requires_book_scope_evidence_and_payoff_test() -> None:
    patch = NarrativePlanPatch(project_id="project-1", target_scope="chapter")

    result = BookPatchValidator().validate(patch)

    assert result.passed is False
    assert "missing_source_evidence" in result.errors
    assert "missing_payoff_test" in result.errors
    assert "unsupported_target_scope:chapter" in result.errors


def test_book_patch_validator_passes_evidenced_book_patch() -> None:
    patch = NarrativePlanPatch(
        project_id="project-1",
        target_scope="book",
        source_obligation_ids=["obl-1"],
        expected_resolution_tests=["终章前完成结构承诺。"],
    )

    result = BookPatchValidator().validate(patch)

    assert result.passed is True
    assert result.errors == []
