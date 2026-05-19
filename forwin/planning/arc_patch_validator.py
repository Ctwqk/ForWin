from __future__ import annotations

from forwin.narrative_obligations.types import NarrativePlanPatch

from .structural_patch_validator import PatchValidationResult, common_structural_patch_errors


class ArcPatchValidator:
    def validate(self, patch: NarrativePlanPatch) -> PatchValidationResult:
        errors = common_structural_patch_errors(patch, expected_scope="arc")
        if not str(patch.target_arc_id or "").strip():
            errors.append("missing_target_arc_id")
        return PatchValidationResult(passed=not errors, errors=errors)
