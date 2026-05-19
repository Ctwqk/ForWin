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
    "operator": "manual_review",
}

MAX_ATTEMPTS_PER_SCOPE: dict[IssueScope, int] = {
    "draft": 2,
    "chapter_plan": 2,
    "band_plan": 2,
    "arc_plan": 1,
    "book_plan": 1,
    "subworld": 2,
    "active_rules": 1,
    "operator": 0,
}

ESCALATION_PATH: tuple[IssueScope, ...] = (
    "draft",
    "chapter_plan",
    "band_plan",
    "arc_plan",
    "book_plan",
)


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
    selected_scope = primary.scope
    max_attempts = MAX_ATTEMPTS_PER_SCOPE.get(selected_scope, 1)
    attempts_for_scope = _attempt_count_for_scope(input, selected_scope)
    escalated_from = ""
    if max_attempts <= 0:
        outcome = "manual_review"
    elif attempts_for_scope >= max_attempts:
        escalated_from = selected_scope
        selected_scope = _escalate_scope(selected_scope) or "operator"
        outcome = (
            "manual_review"
            if selected_scope == "operator"
            else _SCOPE_TO_OUTCOME.get(selected_scope, "manual_review")
        )
    else:
        outcome = _SCOPE_TO_OUTCOME.get(selected_scope, "manual_review")
    return Decision(
        outcome=outcome,
        reason=f"{primary.kind} routes to {selected_scope}",
        rule_id="repair_v2_scope_driven",
        missing_evidence=[] if primary.evidence_refs else ["evidence"],
        routed_from="RepairPolicy.v2",
        sub_action={
            "scope": selected_scope,
            "original_scope": primary.scope,
            "escalated_from": escalated_from,
            "attempts_for_scope": attempts_for_scope,
            "max_attempts_for_scope": max_attempts,
            "issue_kind": primary.kind,
            "severity": primary.severity,
            "source_layer": primary.source_layer,
            "evidence_refs": list(primary.evidence_refs),
        },
    )


def _attempt_count_for_scope(input: DecisionInput, scope: IssueScope) -> int:
    return sum(1 for item in input.prior_scope_history if str(item or "") == scope)


def _escalate_scope(scope: IssueScope) -> IssueScope | None:
    if scope not in ESCALATION_PATH:
        return None
    index = ESCALATION_PATH.index(scope)
    if index >= len(ESCALATION_PATH) - 1:
        return None
    return ESCALATION_PATH[index + 1]
