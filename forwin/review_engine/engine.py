from __future__ import annotations

from .types import Decision, DecisionInput, DecisionRule


class AutoDecisionEngine:
    def __init__(self, rules: list[DecisionRule]) -> None:
        self.rules = sorted(rules, key=lambda item: item.priority)

    def decide(self, input: DecisionInput) -> Decision:
        for rule in self.rules:
            if not rule.matches(input):
                continue
            decision = rule.decide(input)
            if decision.rule_id == rule.rule_id:
                return decision
            return Decision(
                outcome=decision.outcome,
                reason=decision.reason,
                rule_id=rule.rule_id,
                missing_evidence=list(decision.missing_evidence),
                routed_from=decision.routed_from or rule.source_dispatcher,
                sub_action=dict(decision.sub_action),
            )
        return Decision(
            outcome="manual_review",
            reason="no review-engine rule matched",
            rule_id="no_rule_matched",
            missing_evidence=["matching_rule"],
            routed_from="review_engine",
            sub_action={},
        )
