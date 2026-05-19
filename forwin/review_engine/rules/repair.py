from __future__ import annotations

from forwin.reviser.policy import RepairDecision, RepairPolicy

from ..types import Decision, DecisionInput, DecisionRule
from .repair_v2 import build_repair_v2_rules


def build_scope_driven_repair_rules(
    *,
    repair_v2_enabled: bool,
    policy: RepairPolicy | None = None,
) -> list[DecisionRule]:
    return [
        *build_repair_v2_rules(enabled=repair_v2_enabled),
        *build_repair_rules(policy=policy),
    ]


def build_repair_rules(policy: RepairPolicy | None = None) -> list[DecisionRule]:
    resolved_policy = policy or RepairPolicy()
    return [
        DecisionRule(
            rule_id="legacy_repair_policy",
            source_dispatcher="RepairPolicy",
            priority=200,
            matches=lambda input: input.review.verdict == "fail",
            decide=lambda input: decision_from_repair_decision(
                resolved_policy.decide(
                    verdict=input.review.verdict,
                    operation_mode=input.operation_mode,
                    attempts_completed=input.attempts_completed,
                )
            ),
        )
    ]


def decision_from_repair_decision(repair: RepairDecision) -> Decision:
    if repair.kind == "repair":
        return Decision(
            outcome=_outcome_for_scope(repair.scope),
            reason=repair.reason,
            rule_id="legacy_repair_policy",
            missing_evidence=[],
            routed_from="RepairPolicy",
            sub_action={
                "kind": repair.kind,
                "scope": repair.scope,
                "attempt_no": repair.attempt_no,
                "max_attempts": repair.max_attempts,
                "preferred_provider_kind": repair.preferred_provider_kind,
                "preferred_model": repair.preferred_model,
            },
        )
    return Decision(
        outcome="manual_review",
        reason=repair.reason,
        rule_id="legacy_repair_policy",
        missing_evidence=[],
        routed_from="RepairPolicy",
        sub_action={
            "kind": repair.kind,
            "attempt_no": repair.attempt_no,
            "max_attempts": repair.max_attempts,
        },
    )


def _outcome_for_scope(scope: str) -> str:
    normalized = str(scope or "").strip()
    if normalized == "chapter_plan":
        return "chapter_patch"
    if normalized in {"band", "band_plan"}:
        return "band_patch"
    return "local_repair"
