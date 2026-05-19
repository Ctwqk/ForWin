from __future__ import annotations

from forwin.narrative_obligations.types import NarrativePlanPatch

from .structural_patch_validator import PatchValidationResult, common_structural_patch_errors


class BookPatchValidator:
    def validate(self, patch: NarrativePlanPatch) -> PatchValidationResult:
        errors = common_structural_patch_errors(patch, expected_scope="book")
        return PatchValidationResult(passed=not errors, errors=errors)
