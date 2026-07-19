from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.audit.events import DecisionEventInfo


class ChapterDetail(BaseModel):
    chapter_number: int
    title: str
    body: str
    char_count: int
    summary: str
    status: str
    has_draft: bool = False
    has_review: bool = False
    version: int = 1
    acceptance_mode: str = ""
    repair_attempt_count: int = 0
    canon_risk_level: str = ""
    residual_review_issues: list[dict[str, Any]] = Field(default_factory=list)


class ChapterReviewIssueInfo(BaseModel):
    rule_name: str
    severity: str
    description: str
    entity_names: list[str] = Field(default_factory=list)
    issue_type: str = ""
    target_scope: str = ""
    issue_group: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    suggested_fix: str = ""


class LintSignalInfo(BaseModel):
    tool: str
    code: str = ""
    severity: str = "warning"
    message: str
    line: int = 0
    column: int = 0
    evidence_refs: list[str] = Field(default_factory=list)


class RepairVerificationInfo(BaseModel):
    fixed_all_must_fix: bool = False
    preserved_all_must_preserve: bool = False
    unfixed: list[str] = Field(default_factory=list)
    broken_preserve_constraints: list[str] = Field(default_factory=list)
    new_risks: list[str] = Field(default_factory=list)
    verifier_mode: str = ""


class FinalResidualDecisionInfo(BaseModel):
    decision: str = "repair_exhausted"
    forceable: bool = False
    reason: str = ""
    canon_risk: str = ""
    residual_issues: list[str] = Field(default_factory=list)
    requires_human: bool = True


class ChapterRewriteAttemptInfo(BaseModel):
    attempt_no: int
    repair_phase: str = "review_repair"
    phase_attempt_no: int = 0
    repair_scope: str = ""
    result_verdict: str = ""
    result_review_id: str = ""
    failure_reason: str = ""
    forced_accept_applied: bool = False
    design_patch: dict[str, Any] = Field(default_factory=dict)
    verification: RepairVerificationInfo | None = None
    source_chapter_plan: dict[str, Any] = Field(default_factory=dict)
    result_chapter_plan: dict[str, Any] = Field(default_factory=dict)
    source_band_plan: dict[str, Any] = Field(default_factory=dict)
    result_band_plan: dict[str, Any] = Field(default_factory=dict)


ChapterDecisionLayer = Literal[
    "draft_review",
    "repair",
    "residual_eligibility",
    "gate_delegation",
    "canon",
]


class ChapterDecisionLayerInfo(BaseModel):
    model_config = {"frozen": True}

    layer: ChapterDecisionLayer
    status: str
    outcome: str
    summary: str = ""
    blocking: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    decision_refs: list[DecisionEventInfo] = Field(default_factory=list)


class ChapterReviewDetail(BaseModel):
    project_id: str
    chapter_number: int
    title: str
    status: str
    draft_id: str
    version: int
    body: str
    summary: str
    verdict: str
    issues: list[ChapterReviewIssueInfo] = Field(default_factory=list)
    artifact_meta_path: str = ""
    recommended_action: str = ""
    review_summary: str = ""
    planned_reward_tags: list[str] = Field(default_factory=list)
    delivered_reward_tags: list[str] = Field(default_factory=list)
    experience_scores: dict[str, float] = Field(default_factory=dict)
    review_notes: list[str] = Field(default_factory=list)
    lint_signals: list[LintSignalInfo] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confirmed_signal_refs: list[str] = Field(default_factory=list)
    reviewer_mode: str = ""
    proposed_design_patch: dict[str, Any] = Field(default_factory=dict)
    rewrite_attempt_count: int = 0
    latest_repair_scope: str = ""
    latest_repair_scope_reason: str = ""
    forced_accept_applied: bool = False
    acceptance_mode: str = ""
    repair_attempt_count: int = 0
    canon_risk_level: str = ""
    residual_review_issues: list[ChapterReviewIssueInfo] = Field(default_factory=list)
    repair_verification: RepairVerificationInfo | None = None
    final_residual_decision: FinalResidualDecisionInfo | None = None
    repair_exhausted: bool = False
    rewrite_attempts: list[ChapterRewriteAttemptInfo] = Field(default_factory=list)
    decision_refs: list[DecisionEventInfo] = Field(default_factory=list)
    decision_layers: list[ChapterDecisionLayerInfo] = Field(default_factory=list)
    rule_decision: dict[str, Any] = Field(default_factory=dict)


class ChapterReviewApproveRequest(BaseModel):
    continue_generation: bool = False
    reason: str = ""


class ChapterReviewRetryRequest(BaseModel):
    continue_generation: bool = False
    reason: str = ""
    allow_accepted: bool = False


class ChapterReviewApproveResponse(BaseModel):
    ok: bool
    project_id: str
    chapter_number: int
    status: str
    message: str
    task_id: str = ""
    frozen_artifact: str = ""


class TropeTemplateInfo(BaseModel):
    template_id: str
    display_name: str = ""
    category: str
    setup_requirement: str = ""
    payoff_shape: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    best_window: str = ""
    recommended_hook_types: list[str] = Field(default_factory=list)


class TropeRegistrySummaryResponse(BaseModel):
    total_count: int = 0
    category_counts: dict[str, int] = Field(default_factory=dict)
    version: str = "starter"
    source: str = "seed"
    is_full_library: bool = False
    validation_errors: list[str] = Field(default_factory=list)


class TropeTemplateValidationRequest(BaseModel):
    templates: list[dict[str, Any]] = Field(default_factory=list)
    require_full: bool = True


class TropeTemplateValidationResponse(BaseModel):
    ok: bool
    total_count: int = 0
    category_counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class BandExperienceOverrideRequest(BaseModel):
    scheduled_rewards: list[dict[str, Any]] = Field(default_factory=list)
    curiosity_beats: list[dict[str, Any]] = Field(default_factory=list)
    immersion_anchor_scene_goal: str = ""


class BandExperienceOverrideResponse(BaseModel):
    ok: bool
    project_id: str
    band_id: str
    chapter_start: int
    chapter_end: int
    message: str


class CandidateDraftDetail(BaseModel):
    project_id: str
    chapter_number: int
    title: str = ""
    status: str = ""
    candidate_draft_id: str = ""
    version: int = 1
    body: str = ""
    summary: str = ""
    char_count: int = 0
    scene_outputs: list[dict[str, Any]] = Field(default_factory=list)
    state_change_candidates: list[dict[str, Any]] = Field(default_factory=list)
    event_candidates: list[dict[str, Any]] = Field(default_factory=list)
    thread_beat_candidates: list[dict[str, Any]] = Field(default_factory=list)
    review_verdict: str = ""
    review_summary: str = ""
    repair_attempts: list[ChapterRewriteAttemptInfo] = Field(default_factory=list)
    repair_attempt_count: int = 0
    canon_ready: bool = False
    canon_status: str = "candidate"
    canon_artifact_path: str = ""
    failure_reason: str = ""


__all__ = [
    "ChapterDetail",
    "ChapterReviewIssueInfo",
    "LintSignalInfo",
    "RepairVerificationInfo",
    "FinalResidualDecisionInfo",
    "ChapterRewriteAttemptInfo",
    "ChapterDecisionLayer",
    "ChapterDecisionLayerInfo",
    "ChapterReviewDetail",
    "ChapterReviewApproveRequest",
    "ChapterReviewRetryRequest",
    "ChapterReviewApproveResponse",
    "TropeTemplateInfo",
    "TropeRegistrySummaryResponse",
    "TropeTemplateValidationRequest",
    "TropeTemplateValidationResponse",
    "BandExperienceOverrideRequest",
    "BandExperienceOverrideResponse",
    "CandidateDraftDetail",
]
