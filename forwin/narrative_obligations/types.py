from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ObligationStatus = Literal["proposed", "planned", "active", "resolved", "expired", "waived", "blocked"]
ObligationPriority = Literal["P0", "P1", "P2", "P3"]
ObligationHardness = Literal["soft_gap", "design_debt", "canon_risk", "hard_blocker"]
PlanPatchScope = Literal["chapter", "band", "arc", "book"]


class NarrativeObligation(BaseModel):
    id: str = ""
    project_id: str
    origin_chapter_number: int
    origin_draft_id: str = ""
    origin_review_id: str = ""
    origin_signal_ids: list[str] = Field(default_factory=list)
    origin_plan_snapshot_id: str = ""
    obligation_type: str
    priority: ObligationPriority = "P1"
    status: ObligationStatus = "proposed"
    summary: str
    deferral_reason: str = ""
    hardness: ObligationHardness = "soft_gap"
    subject_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    deadline_chapter: int
    deadline_policy: str = "block_at_deadline"
    payoff_test: str
    resolution_conditions: list[str] = Field(default_factory=list)
    linked_plan_patch_ids: list[str] = Field(default_factory=list)
    linked_future_chapters: list[int] = Field(default_factory=list)
    blocking_policy: str = "block_at_deadline"
    created_by: str = "system"
    resolution_chapter: int = 0
    resolution_evidence_refs: list[str] = Field(default_factory=list)
    waive_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    must_resolve_now: bool = False


class NarrativePlanPatch(BaseModel):
    id: str = ""
    project_id: str
    patch_type: str = "defer_acceptance"
    target_scope: PlanPatchScope = "chapter"
    target_plan_id: str = ""
    target_arc_id: str = ""
    target_band_id: str = ""
    affected_chapters: list[int] = Field(default_factory=list)
    source_obligation_ids: list[str] = Field(default_factory=list)
    source_signal_ids: list[str] = Field(default_factory=list)
    old_plan_digest: str = ""
    new_plan_digest: str = ""
    old_contract: dict[str, Any] = Field(default_factory=dict)
    new_contract: dict[str, Any] = Field(default_factory=dict)
    diff_summary: str = ""
    must_preserve: list[str] = Field(default_factory=list)
    must_not_change: list[str] = Field(default_factory=list)
    new_constraints: list[str] = Field(default_factory=list)
    writer_context_injections: list[dict[str, Any]] = Field(default_factory=list)
    reviewer_context_injections: list[dict[str, Any]] = Field(default_factory=list)
    expected_resolution_tests: list[str] = Field(default_factory=list)
    validation_status: Literal["pending", "passed", "failed"] = "pending"
    validation_errors: list[str] = Field(default_factory=list)
    applied: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanPatchValidationResult(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)


class ObligationResolutionCandidate(BaseModel):
    obligation_id: str
    chapter_number: int
    resolution_type: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str = ""
    verifier_result: Literal["pass", "warn", "fail"] = "fail"


class ReviewOutcome(BaseModel):
    action: Literal[
        "commit_clean",
        "commit_with_obligation",
        "local_rewrite",
        "chapter_replan_then_rewrite",
        "band_replan_then_rewrite",
        "arc_replan_then_rewrite",
        "defer_with_chapter_plan_patch",
        "defer_with_band_plan_patch",
        "defer_with_arc_plan_patch",
        "book_replan_required",
        "manual_review_required",
        "block",
    ]
    reason: str = ""
    primary_issue_class: str = ""
    minimum_scope: Literal["draft", "chapter_plan", "band", "arc", "book", "manual"] = "draft"
    obligation_ids: list[str] = Field(default_factory=list)
    blocking_signal_ids: list[str] = Field(default_factory=list)
    plan_patch_ids: list[str] = Field(default_factory=list)
    deadline_chapter: int | None = None
    payoff_test: str = ""
    downgrade_reason: str = ""
