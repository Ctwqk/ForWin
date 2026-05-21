from __future__ import annotations

from forwin.reviser.final_acceptance import FinalAcceptanceGate

from ..types import Decision, DecisionInput, DecisionRule


def build_final_acceptance_rules(gate: FinalAcceptanceGate | None = None) -> list[DecisionRule]:
    resolved_gate = gate or FinalAcceptanceGate()
    return [
        DecisionRule(
            rule_id="final_acceptance_gate",
            source_dispatcher="FinalAcceptanceGate",
            priority=300,
            matches=lambda input: input.review.repair_verification is not None,
            decide=lambda input: _decision_from_final_gate(resolved_gate, input),
        )
    ]


def _decision_from_final_gate(gate: FinalAcceptanceGate, input: DecisionInput) -> Decision:
    result = gate.evaluate(
        operation_mode=input.operation_mode,
        review=input.review,
        verification=input.review.repair_verification,
    )
    return Decision(
        outcome="auto_approve" if result.decision == "force_accept" else "manual_review",
        reason=result.reason,
        rule_id="final_acceptance_gate",
        missing_evidence=[] if result.forceable else ["force_accept_conditions"],
        routed_from="FinalAcceptanceGate",
        sub_action={
            "final_gate_decision": result.decision,
            "forceable": result.forceable,
            "canon_risk": result.canon_risk,
            "residual_issues": list(result.residual_issues),
            "requires_human": result.requires_human,
        },
    )
