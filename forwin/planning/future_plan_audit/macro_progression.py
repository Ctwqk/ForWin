from __future__ import annotations

from forwin.book_state.macro_status import ProtagonistMacroStatus
from forwin.models.project import ArcPlanVersion
from forwin.planning.future_plan_audit.models import FuturePlanAuditIssue
from forwin.planning.macro_progression import load_arc_macro_progression


def audit_arc_macro_boundary(
    *,
    arc: ArcPlanVersion,
    current_chapter: int,
    status: ProtagonistMacroStatus,
) -> list[FuturePlanAuditIssue]:
    if int(current_chapter or 0) < int(arc.chapter_end or 0):
        return []
    progression = load_arc_macro_progression(arc)
    missing: list[str] = []
    if progression.status_tier_to and status.status_tier < progression.status_tier_to:
        missing.append("status_tier")
    if progression.wealth_tier_to and status.wealth_tier < progression.wealth_tier_to:
        missing.append("wealth_tier")
    if progression.enemy_tier_to and status.enemy_tier < progression.enemy_tier_to:
        missing.append("enemy_tier")
    if progression.market_space_to and progression.market_space_to != status.market_space:
        missing.append("market_space")
    if not missing:
        return []
    return [
        FuturePlanAuditIssue(
            issue_type="arc_macro_progression_not_met",
            severity="error",
            target_chapter=int(arc.chapter_end or current_chapter or 0),
            target_plan_id="",
            description=(
                f"Arc {int(arc.arc_number or 0)} ended without required "
                f"macro progression: {', '.join(missing)}."
            ),
            evidence_refs=list(status.evidence_refs),
            patch_type="macro_progression_boundary",
            blocking=True,
            metadata={
                "arc_id": str(arc.id or ""),
                "arc_number": int(arc.arc_number or 0),
                "missing_targets": missing,
                "planned": progression.model_dump(mode="json"),
                "actual": status.model_dump(mode="json"),
            },
        )
    ]


__all__ = ["audit_arc_macro_boundary"]
