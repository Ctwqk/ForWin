from __future__ import annotations

from forwin.narrative_obligations.types import (
    NarrativeObligation,
    NarrativePlanPatch,
    PlanPatchValidationResult,
)


class PlanPatchValidator:
    def validate(
        self,
        *,
        patch: NarrativePlanPatch,
        obligations: list[NarrativeObligation],
        current_chapter: int,
        target_total_chapters: int,
        accepted_chapters: list[int] | None = None,
        unresolved_obligation_ids: list[str] | None = None,
        band_plan_bounds: dict[str, tuple[int, int]] | None = None,
        minimum_scope_by_obligation: dict[str, str] | None = None,
    ) -> PlanPatchValidationResult:
        errors: list[str] = []
        source_ids = set(patch.source_obligation_ids)
        affected = [int(chapter) for chapter in patch.affected_chapters]
        current = int(current_chapter or 0)
        target_total = int(target_total_chapters or 0)
        accepted = {int(chapter) for chapter in accepted_chapters or []}
        current_chapter_patch_types = {"canon_plan_staleness"}
        band_bounds = band_plan_bounds or {}
        minimum_scopes = minimum_scope_by_obligation or {}

        if not affected:
            errors.append("missing_affected_chapters")
        for chapter in affected:
            if chapter <= current and patch.patch_type not in current_chapter_patch_types:
                errors.append(f"affected_chapter_not_future:{chapter}")
            if target_total and chapter > target_total:
                errors.append(f"affected_chapter_after_final:{chapter}")
            if chapter in accepted:
                errors.append(f"affected_chapter_already_accepted:{chapter}")

        if not patch.writer_context_injections:
            errors.append("missing_writer_context_injections")
        if not patch.reviewer_context_injections:
            errors.append("missing_reviewer_context_injections")
        if not patch.expected_resolution_tests:
            errors.append("missing_expected_resolution_tests")

        if patch.target_scope == "band":
            if not str(patch.target_band_id or "").strip():
                errors.append("missing_target_band_id")
            contract = patch.new_contract.get("band_obligation_contract")
            if not isinstance(contract, dict):
                errors.append("missing_band_obligation_contract")
            bounds = band_bounds.get(str(patch.target_band_id or ""))
            if bounds is not None:
                start, end = int(bounds[0]), int(bounds[1])
                for chapter in affected:
                    if chapter < start or chapter > end:
                        errors.append(f"affected_chapter_outside_band:{chapter}:{patch.target_band_id}")
            if isinstance(contract, dict):
                carry_forward = {
                    str(item).strip()
                    for item in contract.get("allowed_carry_forward", [])
                    if str(item).strip()
                }
                priority_by_id = {item.id: item.priority for item in obligations if item.id}
                for obligation_id in sorted(carry_forward):
                    if priority_by_id.get(obligation_id) in {"P0", "P1"}:
                        errors.append(f"p0_p1_obligation_cannot_carry_forward:{obligation_id}")

        for obligation in obligations:
            if obligation.id not in source_ids:
                errors.append(f"missing_source_obligation:{obligation.id}")
            if not obligation.payoff_test.strip():
                errors.append(f"missing_payoff_test:{obligation.id}")
            if obligation.deadline_chapter <= current:
                errors.append(f"deadline_not_future:{obligation.id}")
            if target_total and obligation.deadline_chapter > target_total:
                errors.append(f"deadline_after_final:{obligation.id}")
            if affected and max(affected) > obligation.deadline_chapter:
                errors.append(f"patch_after_obligation_deadline:{obligation.id}")
            minimum_scope = str(
                minimum_scopes.get(obligation.id)
                or (obligation.metadata.get("minimum_scope") if isinstance(obligation.metadata, dict) else "")
                or ""
            ).strip()
            if minimum_scope and _scope_rank(patch.target_scope) < _scope_rank(minimum_scope):
                errors.append(
                    f"patch_scope_below_obligation_minimum:{obligation.id}:{patch.target_scope}<{minimum_scope}"
                )

        removed = {
            str(item).strip()
            for item in (patch.metadata.get("removed_obligation_ids", []) if isinstance(patch.metadata, dict) else [])
            if str(item).strip()
        }
        new_contract_removed = patch.new_contract.get("remove_obligation_ids", [])
        if isinstance(new_contract_removed, list):
            removed.update(str(item).strip() for item in new_contract_removed if str(item).strip())
        unresolved = {str(item).strip() for item in unresolved_obligation_ids or [] if str(item).strip()}
        for obligation_id in sorted(removed & unresolved):
            errors.append(f"removes_unresolved_obligation:{obligation_id}")

        return PlanPatchValidationResult(passed=not errors, errors=errors)


def _scope_rank(scope: str) -> int:
    return {
        "draft": 0,
        "scene": 0,
        "chapter": 1,
        "chapter_plan": 1,
        "band": 2,
        "band_plan": 2,
        "arc": 3,
        "book": 4,
        "manual": 5,
    }.get(str(scope or "").strip().lower(), 0)
