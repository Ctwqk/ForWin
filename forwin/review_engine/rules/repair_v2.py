from __future__ import annotations

from dataclasses import dataclass

from forwin.review_engine.issue_taxonomy import IssueScope, classify_primary_issue
from forwin.review_engine.types import Decision, DecisionInput, DecisionOutcome, DecisionRule

_SCOPE_TO_OUTCOME: dict[IssueScope, DecisionOutcome] = {
    "draft": "local_repair",
    "chapter_plan": "chapter_patch",
    "band_plan": "band_patch",
    "arc_plan": "arc_patch",
    "book_plan": "book_patch",
    "subworld": "chapter_patch",
    "active_rules": "chapter_patch",
    "operator": "system_block",
}


@dataclass(frozen=True)
class RepairV2ShadowResult:
    live_scope: str
    shadow_scope: str
    enabled: bool


def compare_repair_v2_shadow(
    *,
    old_scope: str,
    new_scope: str,
    enabled: bool,
) -> RepairV2ShadowResult:
    return RepairV2ShadowResult(
        live_scope=str(new_scope if enabled else old_scope),
        shadow_scope=str(new_scope),
        enabled=bool(enabled),
    )


def build_repair_v2_rules(*, enabled: bool) -> list[DecisionRule]:
    return [
        DecisionRule(
            rule_id="repair_v2_scope_driven",
            source_dispatcher="RepairPolicy.v2",
            priority=80,
            matches=lambda input: bool(enabled) and input.review.verdict == "fail",
            decide=decide_repair_v2,
        )
    ]


def decide_repair_v2(input: DecisionInput) -> Decision:
    primary = classify_primary_issue(review=input.review, signals=input.signals)
    outcome = _SCOPE_TO_OUTCOME.get(primary.scope, "manual_review")
    return Decision(
        outcome=outcome,
        reason=f"{primary.kind} routes to {primary.scope}",
        rule_id="repair_v2_scope_driven",
        missing_evidence=[] if primary.evidence_refs else ["evidence"],
        routed_from="RepairPolicy.v2",
        sub_action={
            "scope": primary.scope,
            "issue_kind": primary.kind,
            "severity": primary.severity,
            "source_layer": primary.source_layer,
            "evidence_refs": list(primary.evidence_refs),
        },
    )
