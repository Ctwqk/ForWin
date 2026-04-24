from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, Field

from forwin.protocol.context import LintSignal
from forwin.protocol.experience import RewardTag


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
    repair_scope: Literal["scene", "band", "arc"]
    failure_type: Literal[
        "continuity",
        "immersion",
        "payoff_miss",
        "stall",
        "hook_failure",
        "mixed",
    ]
    must_fix: list[str] = Field(default_factory=list)
    must_preserve: list[str] = Field(default_factory=list)
    scope_reason: str = ""
    design_patch: dict[str, object] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


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
    forced_accept_applied: bool = False
