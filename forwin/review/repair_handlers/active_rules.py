from __future__ import annotations

from dataclasses import dataclass, field

from forwin.canon_quality.active_rule_store import ActiveRulePatch, ActiveRuleStore
from forwin.canon_quality.active_rules_handler import apply_pre_write_active_rules


@dataclass(slots=True)
class ActiveRulesRepairReport:
    applied: int = 0
    rejected: int = 0
    applied_rule_keys: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)


def apply_active_rules_repair(
    *,
    project_id: str,
    chapter_number: int,
    patches: list[ActiveRulePatch],
    store: ActiveRuleStore,
) -> ActiveRulesRepairReport:
    report = apply_pre_write_active_rules(
        project_id=project_id,
        chapter_number=chapter_number,
        patches=patches,
        store=store,
    )
    return ActiveRulesRepairReport(
        applied=report.applied,
        rejected=report.rejected,
        applied_rule_keys=list(report.applied_rule_keys),
        rejection_reasons=list(report.rejection_reasons),
    )


__all__ = ["ActiveRulesRepairReport", "apply_active_rules_repair"]
