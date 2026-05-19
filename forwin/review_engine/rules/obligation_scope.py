from __future__ import annotations

from forwin.planning.obligation_scope_router import ObligationScopeDecision

from ..types import Decision


def decision_from_obligation_scope(scope: ObligationScopeDecision) -> Decision:
    return Decision(
        outcome=_outcome_for_scope_action(scope.action),
        reason=scope.reason,
        rule_id="legacy_obligation_scope_router",
        missing_evidence=[],
        routed_from="ObligationScopeRouter",
        sub_action={
            "legacy_action": scope.action,
            "target_scope": scope.target_scope,
            "target_band_id": scope.target_band_id,
            "target_arc_id": scope.target_arc_id,
            "affected_chapters": list(scope.affected_chapters),
            "deadline_chapter": scope.deadline_chapter,
        },
    )


def _outcome_for_scope_action(action: str) -> str:
    normalized = str(action or "").strip()
    if normalized == "defer_with_chapter_plan_patch":
        return "chapter_patch"
    if normalized == "defer_with_band_plan_patch":
        return "band_patch"
    if normalized == "block":
        return "system_block"
    return "manual_review"
