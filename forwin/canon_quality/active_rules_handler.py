from __future__ import annotations

from dataclasses import dataclass, field

from .active_rule_store import ActiveRulePatch, ActiveRuleStore


@dataclass(slots=True)
class ActiveRuleApplyReport:
    applied: int = 0
    rejected: int = 0
    applied_rule_keys: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)


def apply_pre_write_active_rules(
    *,
    project_id: str,
    chapter_number: int,
    patches: list[ActiveRulePatch],
    store: ActiveRuleStore,
) -> ActiveRuleApplyReport:
    report = ActiveRuleApplyReport()
    for patch in patches:
        reason = _patch_rejection_reason(patch, chapter_number=chapter_number)
        if reason:
            report.rejected += 1
            report.rejection_reasons.append(reason)
            continue
        result = store.register_rule(
            project_id=project_id,
            rule=patch.rule,
            trigger_quote=patch.trigger_quote,
        )
        if result.applied:
            report.applied += 1
            report.applied_rule_keys.append(patch.rule.rule_key)
        else:
            report.rejected += 1
            report.rejection_reasons.append(result.reason or "active_rule_registration_failed")
    return report


def _patch_rejection_reason(patch: ActiveRulePatch, *, chapter_number: int) -> str:
    if not patch.rule.rule_key.strip():
        return "missing_rule_key"
    if not patch.trigger_quote.quote.strip():
        return "missing_trigger_quote"
    if int(patch.trigger_quote.chapter_number or 0) >= int(chapter_number or 0):
        return "trigger_quote_not_from_accepted_prior_chapter"
    return ""


__all__ = ["ActiveRuleApplyReport", "apply_pre_write_active_rules"]
