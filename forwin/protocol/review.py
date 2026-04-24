from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from forwin.protocol.context import LintSignal
from forwin.protocol.experience import RewardTag

RepairScope = Literal[
    "draft",
    "chapter_plan",
    "band_plan",
    "scene",
    "chapter",
    "band",
    "arc",
    "world_model",
]
RepairFailureType = Literal[
    "continuity",
    "immersion",
    "payoff_miss",
    "stall",
    "hook_failure",
    "mixed",
    "character_omniscience",
    "missing_delta_source",
    "missing_world_line",
    "early_reveal",
    "unpaid_promise_debt",
    "cognition_conflict",
    "world_model_conflict",
]
FinalGateDecisionKind = Literal[
    "force_accept",
    "manual_review_required",
    "repair_exhausted",
]
CanonRiskLevel = Literal["low", "high"]

_LEGACY_REPAIR_SCOPE_MAP = {
    "scene": "draft",
    "band": "chapter_plan",
    "arc": "band_plan",
}
_V4_REPAIR_SCOPES = {"scene", "chapter", "band", "arc", "world_model"}
_KNOWN_REPAIR_SCOPES = {"draft", "chapter_plan", "band_plan", *_V4_REPAIR_SCOPES}


def normalize_repair_scope(
    value: object,
    *,
    default: str = "draft",
    preserve_v4: bool = False,
) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if preserve_v4 and normalized in _V4_REPAIR_SCOPES:
        return normalized
    return _LEGACY_REPAIR_SCOPE_MAP.get(normalized, normalized)


class ContinuityIssue(BaseModel):
    """A single continuity or quality issue."""
    rule_name: str
    severity: Literal["error", "warning", "info"] = "warning"
    description: str          # In Chinese
    entity_names: list[str] = Field(default_factory=list)
    reviewer: str = ""
    issue_type: str = ""
    target_scope: str = ""
    issue_group: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    suggested_fix: str = ""


class RepairInstruction(BaseModel):
    repair_scope: RepairScope
    failure_type: RepairFailureType
    must_fix: list[str] = Field(default_factory=list)
    must_preserve: list[str] = Field(default_factory=list)
    must_not_reveal: list[str] = Field(default_factory=list)
    required_delta_patch: dict[str, object] = Field(default_factory=dict)
    required_belief_patch: dict[str, object] = Field(default_factory=dict)
    required_hint_patch: dict[str, object] = Field(default_factory=dict)
    required_payoff_patch: dict[str, object] = Field(default_factory=dict)
    scope_reason: str = ""
    design_patch: dict[str, object] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("repair_scope", mode="before")
    @classmethod
    def _normalize_repair_scope(cls, value: object) -> str:
        normalized = normalize_repair_scope(value, preserve_v4=True)
        if normalized not in _KNOWN_REPAIR_SCOPES:
            raise ValueError(f"Unsupported repair scope: {value}")
        return normalized


class RepairVerification(BaseModel):
    fixed_all_must_fix: bool = False
    preserved_all_must_preserve: bool = False
    unfixed: list[str] = Field(default_factory=list)
    broken_preserve_constraints: list[str] = Field(default_factory=list)
    new_risks: list[str] = Field(default_factory=list)
    verifier_mode: str = ""


class FinalGateDecision(BaseModel):
    decision: FinalGateDecisionKind = "repair_exhausted"
    forceable: bool = False
    reason: str = ""
    canon_risk: CanonRiskLevel = "high"
    residual_issues: list[str] = Field(default_factory=list)
    requires_human: bool = True


class ReviewVerdict(BaseModel):
    """Result of continuity checking."""
    verdict: Literal["pass", "warn", "fail"]
    issues: list[ContinuityIssue] = Field(default_factory=list)
    recommended_action: str = ""
    review_summary: str = ""
    planned_reward_tags: list[RewardTag] = Field(default_factory=list)
    delivered_reward_tags: list[RewardTag] = Field(default_factory=list)
    experience_scores: dict[str, float] = Field(default_factory=dict)
    review_notes: list[str] = Field(default_factory=list)
    lint_signals: list[LintSignal] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confirmed_signal_refs: list[str] = Field(default_factory=list)
    reviewer_mode: str = ""
    repair_instruction: RepairInstruction | None = None
    repair_verification: RepairVerification | None = None
    final_gate_decision: FinalGateDecision | None = None
    repair_exhausted: bool = False
    residual_review_issues: list[ContinuityIssue] = Field(default_factory=list)
    forced_accept_applied: bool = False
    prompt_trace: dict[str, object] = Field(default_factory=dict)
    extracted_actuals: dict[str, object] = Field(default_factory=dict)
    approved_delta_refs: list[str] = Field(default_factory=list)
    rejected_delta_refs: list[str] = Field(default_factory=list)
    compiler_gate_status: str = ""
