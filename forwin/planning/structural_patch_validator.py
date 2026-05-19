from __future__ import annotations

from dataclasses import dataclass, field

from forwin.narrative_obligations.types import NarrativePlanPatch


@dataclass(frozen=True)
class PatchValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)


def common_structural_patch_errors(
    patch: NarrativePlanPatch,
    *,
    expected_scope: str,
) -> list[str]:
    errors: list[str] = []
    if not patch.source_signal_ids and not patch.source_obligation_ids:
        errors.append("missing_source_evidence")
    if not patch.expected_resolution_tests:
        errors.append("missing_payoff_test")
    if patch.target_scope != expected_scope:
        errors.append(f"unsupported_target_scope:{patch.target_scope}")
    return errors
