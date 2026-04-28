from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from forwin.protocol.experience import BandDelightSchedule


ProgressionMode = Literal["legacy_relaxed", "serial_canon", "serial_canon_band_guard"]
BandWarnAction = Literal["pause"]
PlanTaskType = Literal[
    "plot_advance",
    "relationship_shift",
    "setup",
    "withhold",
    "experience_delivery",
]
ConstraintType = Literal[
    "character_availability",
    "secret_withhold",
    "relationship_preserve",
    "thread_keep_open",
    "location_availability",
    "rule_preserve",
]
ConstraintLevel = Literal["hard", "soft", "hint"]
ConstraintStatus = Literal["active", "inactive", "archived"]
CheckpointStatus = Literal["pending", "pass", "warn", "fail", "error", "overridden"]
BlockingReasonCode = Literal[
    "",
    "chapter_not_canon",
    "band_checkpoint_pending",
    "band_checkpoint_warn",
    "band_checkpoint_fail",
    "future_constraint_block",
]
DecisionEventFamily = Literal[
    "business_event",
    "audit_action",
    "runtime_observation",
    "evaluation_verdict",
]
DecisionActorType = Literal["system", "scheduler", "manual_ui", "api", "extension"]
OverClosureRiskCategory = Literal[
    "",
    "character_locked_out",
    "thread_closed_too_early",
    "relationship_closed_too_early",
    "secret_over_explained",
    "growth_arc_completed_too_early",
]
IssueGroup = Literal[
    "",
    "fact_conflict",
    "director_imbalance",
    "runtime_observation",
    "governance_action",
]


class DecisionEventType:
    GENERATION_REQUESTED = "generation_requested"
    CONTINUE_REQUESTED = "continue_requested"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_COMPLETED_WITH_FAILURES = "run_completed_with_failures"
    PROJECT_CREATED = "project_created"
    GENESIS_CREATED = "genesis_created"
    GENESIS_UPDATED = "genesis_updated"
    GENESIS_STAGE_GENERATED = "genesis_stage_generated"
    GENESIS_STAGE_LOCKED = "genesis_stage_locked"
    GENESIS_STAGE_RERUN = "genesis_stage_rerun"
    GENESIS_STAGE_REFINED = "genesis_stage_refined"
    START_WRITING_REQUESTED = "start_writing_requested"
    PROMPT_TRACE_RECORDED = "prompt_trace_recorded"

    GOVERNANCE_UPDATED = "governance_updated"
    MANUAL_CHECKPOINT_CREATED = "manual_checkpoint_created"
    MANUAL_CHECKPOINT_HIT = "manual_checkpoint_hit"
    CONSTRAINT_CREATED = "constraint_created"
    CONSTRAINT_UPDATED = "constraint_updated"
    CONSTRAINT_ARCHIVED = "constraint_archived"
    PLAN_TASK_CONTRACT_UPDATED = "plan_task_contract_updated"

    PAUSE_REQUESTED = "pause_requested"
    PAUSE_REACHED = "pause_reached"
    TERMINATE_REQUESTED = "terminate_requested"
    TERMINATE_REACHED = "terminate_reached"

    REVIEW_VERDICT_RECORDED = "review_verdict_recorded"
    REPAIR_STARTED = "repair_started"
    REPAIR_FAILED = "repair_failed"
    REPAIR_SUCCEEDED = "repair_succeeded"
    REVIEW_APPROVED = "review_approved"
    FORCED_ACCEPT_APPLIED = "forced_accept_applied"

    BAND_CHECKPOINT_CREATED = "band_checkpoint_created"
    BAND_CHECKPOINT_HIT = "band_checkpoint_hit"
    CHECKPOINT_EVALUATOR_ERROR = "checkpoint_evaluator_error"
    BAND_CHECKPOINT_APPROVED = "band_checkpoint_approved"
    BAND_CHECKPOINT_OVERRIDDEN = "band_checkpoint_overridden"

    CANON_COMMIT = "canon_commit"
    CANON_COMMIT_FAILED = "canon_commit_failed"
    HARD_GATE_HIT = "hard_gate_hit"

    STAGE_ENTERED = "stage_entered"
    STAGE_EXITED = "stage_exited"
    STAGE_DURATION_SUMMARY = "stage_duration_summary"
    SCENARIO_REHEARSAL_EVALUATED = "scenario_rehearsal_evaluated"
    SCENARIO_REHEARSAL_PATCH_APPLIED = "scenario_rehearsal_patch_applied"
    SCENARIO_REHEARSAL_REPLAN_REQUIRED = "scenario_rehearsal_replan_required"
    SCENARIO_REHEARSAL_BLOCKED = "scenario_rehearsal_blocked"
    PROVISIONAL_GATE_EVALUATED = "provisional_gate_evaluated"
    LLM_REQUEST_STARTED = "llm_request_started"
    LLM_REQUEST_SUCCEEDED = "llm_request_succeeded"
    LLM_REQUEST_FAILED = "llm_request_failed"
    RETRY_ATTEMPT = "retry_attempt"
    FALLBACK_PROFILE_SWITCHED = "fallback_profile_switched"
    MEMORY_INDEX_UPSERT_STARTED = "memory_index_upsert_started"
    MEMORY_INDEX_UPSERT_SUCCEEDED = "memory_index_upsert_succeeded"
    MEMORY_INDEX_UPSERT_FAILED = "memory_index_upsert_failed"
    MAP_GENERATION_STARTED = "map_generation_started"
    MAP_GENERATION_SUCCEEDED = "map_generation_succeeded"
    MAP_GENERATION_FAILED = "map_generation_failed"
    MAP_EXPANSION_STARTED = "map_expansion_started"
    MAP_EXPANSION_SUCCEEDED = "map_expansion_succeeded"
    MAP_EXPANSION_FAILED = "map_expansion_failed"
    MAP_MOVEMENT_REVIEW_ISSUE = "map_movement_review_issue"
    WORLD_MODEL_COMPILE_STARTED = "world_model_compile_started"
    WORLD_MODEL_COMPILE_SUCCEEDED = "world_model_compile_succeeded"
    WORLD_MODEL_COMPILE_FAILED = "world_model_compile_failed"
    KNOWLEDGE_PROJECTION_REFRESHED = "knowledge_projection_refreshed"
    BOOK_STATE_REVIEW_STARTED = "book_state_review_started"
    BOOK_STATE_REVIEW_SUCCEEDED = "book_state_review_succeeded"
    BOOK_STATE_REVIEW_FAILED = "book_state_review_failed"
    BOOK_STATE_COMPILE_STARTED = "book_state_compile_started"
    BOOK_STATE_COMPILE_SUCCEEDED = "book_state_compile_succeeded"
    BOOK_STATE_COMPILE_FAILED = "book_state_compile_failed"
    PERSONALITY_LOADOUT_UPDATED = "personality_loadout_updated"
    LEGACY_PROJECTION_FAILED = "legacy_projection_failed"
    LEGACY_REGION_PROMOTION_STARTED = "legacy_region_promotion_started"
    LEGACY_REGION_PROMOTION_SUCCEEDED = "legacy_region_promotion_succeeded"
    LEGACY_REGION_PROMOTION_FAILED = "legacy_region_promotion_failed"
    TASK_OPERATION_STARTED = "task_operation_started"
    TASK_OPERATION_SUCCEEDED = "task_operation_succeeded"
    TASK_OPERATION_FAILED = "task_operation_failed"
    TASK_CLEANUP_STARTED = "task_cleanup_started"
    TASK_CLEANUP_FINISHED = "task_cleanup_finished"
    CONTEXT_ASSEMBLED = "context_assembled"
    CONTEXT_PRUNED = "context_pruned"
    MEMORY_SEARCH_STARTED = "memory_search_started"
    MEMORY_SEARCH_SUCCEEDED = "memory_search_succeeded"
    MEMORY_SEARCH_FAILED = "memory_search_failed"
    CHAPTER_WRITE_STARTED = "chapter_write_started"
    WRITER_OUTPUT_BUILT = "writer_output_built"
    WRITER_SCENE_FALLBACK_USED = "writer_scene_fallback_used"
    WRITER_PREVIEW_FALLBACK_STARTED = "writer_preview_fallback_started"
    WRITER_PREVIEW_FALLBACK_ATTEMPT_FAILED = "writer_preview_fallback_attempt_failed"
    WRITER_PREVIEW_FALLBACK_SUCCEEDED = "writer_preview_fallback_succeeded"
    WRITER_PREVIEW_FALLBACK_FAILED = "writer_preview_fallback_failed"
    WRITER_OUTPUT_ARTIFACT_SAVED = "writer_output_artifact_saved"
    REVIEW_STARTED = "review_started"
    CANON_COMMIT_STARTED = "canon_commit_started"
    LLM_RESPONSE_PARSE_FAILED = "llm_response_parse_failed"
    ARTIFACT_SAVED = "artifact_saved"
    PROJECT_DELETE_REQUESTED = "project_delete_requested"
    PROJECT_DELETE_STARTED = "project_delete_started"
    PROJECT_DELETE_SUCCEEDED = "project_delete_succeeded"
    PROJECT_DELETE_FAILED = "project_delete_failed"
    AUDIT_BUNDLE_EXPORTED = "audit_bundle_exported"
    PERFORMANCE_WARNING = "performance_warning"
    EXTENSION_HEARTBEAT_RECEIVED = "extension_heartbeat_received"
    BROWSER_SESSION_SYNCED = "browser_session_synced"
    UPLOAD_JOB_CREATED = "upload_job_created"
    UPLOAD_JOB_CLAIMED = "upload_job_claimed"
    UPLOAD_JOB_PROGRESS = "upload_job_progress"
    UPLOAD_JOB_SUCCEEDED = "upload_job_succeeded"
    UPLOAD_JOB_FAILED = "upload_job_failed"
    UPLOAD_JOB_CANCELLED = "upload_job_cancelled"
    COMMENT_SYNC_JOB_CREATED = "comment_sync_job_created"
    COMMENT_SYNC_JOB_CLAIMED = "comment_sync_job_claimed"
    COMMENT_SYNC_SUCCEEDED = "comment_sync_succeeded"
    COMMENT_SYNC_FAILED = "comment_sync_failed"
    RAW_COMMENTS_INGESTED = "raw_comments_ingested"


KNOWN_DECISION_EVENT_TYPES = {
    DecisionEventType.GENERATION_REQUESTED,
    DecisionEventType.CONTINUE_REQUESTED,
    DecisionEventType.RUN_STARTED,
    DecisionEventType.RUN_COMPLETED,
    DecisionEventType.RUN_COMPLETED_WITH_FAILURES,
    DecisionEventType.PROJECT_CREATED,
    DecisionEventType.GENESIS_CREATED,
    DecisionEventType.GENESIS_UPDATED,
    DecisionEventType.GENESIS_STAGE_GENERATED,
    DecisionEventType.GENESIS_STAGE_LOCKED,
    DecisionEventType.GENESIS_STAGE_RERUN,
    DecisionEventType.GENESIS_STAGE_REFINED,
    DecisionEventType.START_WRITING_REQUESTED,
    DecisionEventType.PROMPT_TRACE_RECORDED,
    DecisionEventType.GOVERNANCE_UPDATED,
    DecisionEventType.MANUAL_CHECKPOINT_CREATED,
    DecisionEventType.MANUAL_CHECKPOINT_HIT,
    DecisionEventType.CONSTRAINT_CREATED,
    DecisionEventType.CONSTRAINT_UPDATED,
    DecisionEventType.CONSTRAINT_ARCHIVED,
    DecisionEventType.PLAN_TASK_CONTRACT_UPDATED,
    DecisionEventType.PAUSE_REQUESTED,
    DecisionEventType.PAUSE_REACHED,
    DecisionEventType.TERMINATE_REQUESTED,
    DecisionEventType.TERMINATE_REACHED,
    DecisionEventType.REVIEW_VERDICT_RECORDED,
    DecisionEventType.REPAIR_STARTED,
    DecisionEventType.REPAIR_FAILED,
    DecisionEventType.REPAIR_SUCCEEDED,
    DecisionEventType.REVIEW_APPROVED,
    DecisionEventType.FORCED_ACCEPT_APPLIED,
    DecisionEventType.BAND_CHECKPOINT_CREATED,
    DecisionEventType.BAND_CHECKPOINT_HIT,
    DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
    DecisionEventType.BAND_CHECKPOINT_APPROVED,
    DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN,
    DecisionEventType.CANON_COMMIT,
    DecisionEventType.CANON_COMMIT_FAILED,
    DecisionEventType.HARD_GATE_HIT,
    DecisionEventType.STAGE_ENTERED,
    DecisionEventType.STAGE_EXITED,
    DecisionEventType.STAGE_DURATION_SUMMARY,
    DecisionEventType.SCENARIO_REHEARSAL_EVALUATED,
    DecisionEventType.SCENARIO_REHEARSAL_PATCH_APPLIED,
    DecisionEventType.SCENARIO_REHEARSAL_REPLAN_REQUIRED,
    DecisionEventType.SCENARIO_REHEARSAL_BLOCKED,
    DecisionEventType.PROVISIONAL_GATE_EVALUATED,
    DecisionEventType.LLM_REQUEST_STARTED,
    DecisionEventType.LLM_REQUEST_SUCCEEDED,
    DecisionEventType.LLM_REQUEST_FAILED,
    DecisionEventType.RETRY_ATTEMPT,
    DecisionEventType.FALLBACK_PROFILE_SWITCHED,
    DecisionEventType.MEMORY_INDEX_UPSERT_STARTED,
    DecisionEventType.MEMORY_INDEX_UPSERT_SUCCEEDED,
    DecisionEventType.MEMORY_INDEX_UPSERT_FAILED,
    DecisionEventType.MAP_GENERATION_STARTED,
    DecisionEventType.MAP_GENERATION_SUCCEEDED,
    DecisionEventType.MAP_GENERATION_FAILED,
    DecisionEventType.MAP_EXPANSION_STARTED,
    DecisionEventType.MAP_EXPANSION_SUCCEEDED,
    DecisionEventType.MAP_EXPANSION_FAILED,
    DecisionEventType.MAP_MOVEMENT_REVIEW_ISSUE,
    DecisionEventType.WORLD_MODEL_COMPILE_STARTED,
    DecisionEventType.WORLD_MODEL_COMPILE_SUCCEEDED,
    DecisionEventType.WORLD_MODEL_COMPILE_FAILED,
    DecisionEventType.KNOWLEDGE_PROJECTION_REFRESHED,
    DecisionEventType.BOOK_STATE_REVIEW_STARTED,
    DecisionEventType.BOOK_STATE_REVIEW_SUCCEEDED,
    DecisionEventType.BOOK_STATE_REVIEW_FAILED,
    DecisionEventType.BOOK_STATE_COMPILE_STARTED,
    DecisionEventType.BOOK_STATE_COMPILE_SUCCEEDED,
    DecisionEventType.BOOK_STATE_COMPILE_FAILED,
    DecisionEventType.PERSONALITY_LOADOUT_UPDATED,
    DecisionEventType.LEGACY_PROJECTION_FAILED,
    DecisionEventType.LEGACY_REGION_PROMOTION_STARTED,
    DecisionEventType.LEGACY_REGION_PROMOTION_SUCCEEDED,
    DecisionEventType.LEGACY_REGION_PROMOTION_FAILED,
    DecisionEventType.TASK_OPERATION_STARTED,
    DecisionEventType.TASK_OPERATION_SUCCEEDED,
    DecisionEventType.TASK_OPERATION_FAILED,
    DecisionEventType.TASK_CLEANUP_STARTED,
    DecisionEventType.TASK_CLEANUP_FINISHED,
    DecisionEventType.CONTEXT_ASSEMBLED,
    DecisionEventType.CONTEXT_PRUNED,
    DecisionEventType.MEMORY_SEARCH_STARTED,
    DecisionEventType.MEMORY_SEARCH_SUCCEEDED,
    DecisionEventType.MEMORY_SEARCH_FAILED,
    DecisionEventType.CHAPTER_WRITE_STARTED,
    DecisionEventType.WRITER_OUTPUT_BUILT,
    DecisionEventType.WRITER_SCENE_FALLBACK_USED,
    DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED,
    DecisionEventType.WRITER_PREVIEW_FALLBACK_ATTEMPT_FAILED,
    DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED,
    DecisionEventType.WRITER_PREVIEW_FALLBACK_FAILED,
    DecisionEventType.WRITER_OUTPUT_ARTIFACT_SAVED,
    DecisionEventType.REVIEW_STARTED,
    DecisionEventType.CANON_COMMIT_STARTED,
    DecisionEventType.LLM_RESPONSE_PARSE_FAILED,
    DecisionEventType.ARTIFACT_SAVED,
    DecisionEventType.PROJECT_DELETE_REQUESTED,
    DecisionEventType.PROJECT_DELETE_STARTED,
    DecisionEventType.PROJECT_DELETE_SUCCEEDED,
    DecisionEventType.PROJECT_DELETE_FAILED,
    DecisionEventType.AUDIT_BUNDLE_EXPORTED,
    DecisionEventType.PERFORMANCE_WARNING,
    DecisionEventType.EXTENSION_HEARTBEAT_RECEIVED,
    DecisionEventType.BROWSER_SESSION_SYNCED,
    DecisionEventType.UPLOAD_JOB_CREATED,
    DecisionEventType.UPLOAD_JOB_CLAIMED,
    DecisionEventType.UPLOAD_JOB_PROGRESS,
    DecisionEventType.UPLOAD_JOB_SUCCEEDED,
    DecisionEventType.UPLOAD_JOB_FAILED,
    DecisionEventType.UPLOAD_JOB_CANCELLED,
    DecisionEventType.COMMENT_SYNC_JOB_CREATED,
    DecisionEventType.COMMENT_SYNC_JOB_CLAIMED,
    DecisionEventType.COMMENT_SYNC_SUCCEEDED,
    DecisionEventType.COMMENT_SYNC_FAILED,
    DecisionEventType.RAW_COMMENTS_INGESTED,
}

PLAN_TASK_TYPES = {
    "plot_advance",
    "relationship_shift",
    "setup",
    "withhold",
    "experience_delivery",
}
CONSTRAINT_TYPES = {
    "character_availability",
    "secret_withhold",
    "relationship_preserve",
    "thread_keep_open",
    "location_availability",
    "rule_preserve",
}
CONSTRAINT_LEVELS = {"hard", "soft", "hint"}
CONSTRAINT_STATUSES = {"active", "inactive", "archived"}


class PlanTaskItem(BaseModel):
    task_type: PlanTaskType
    description: str = ""
    target_name: str = ""
    required_keywords: list[str] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)
    source: str = "derived"


class ProjectGovernanceSettings(BaseModel):
    default_operation_mode: str = "blackbox"
    review_interval_chapters: int = 0
    progression_mode: ProgressionMode = "legacy_relaxed"
    auto_band_checkpoint: bool = False
    band_warn_action: BandWarnAction = "pause"
    manual_checkpoints_enabled: bool = False
    future_constraints_enabled: bool = False


class BlockingReasonInfo(BaseModel):
    code: BlockingReasonCode = ""
    message: str = ""
    chapter_number: int = 0
    band_id: str = ""
    decision_event_id: str = ""
    detail: str = ""


class NarrativeConstraintInfo(BaseModel):
    id: str = ""
    project_id: str = ""
    arc_id: str = ""
    band_id: str = ""
    constraint_type: ConstraintType = "character_availability"
    level: ConstraintLevel = "hard"
    subject_name: str = ""
    description: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    effective_from_chapter: int = 1
    protect_until_chapter: int = 0
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


class DecisionEventInfo(BaseModel):
    id: str = ""
    project_id: str = ""
    task_id: str = ""
    band_id: str = ""
    chapter_number: int = 0
    scope: str = "project"
    event_family: DecisionEventFamily = "business_event"
    event_type: str = ""
    actor_type: DecisionActorType = "system"
    actor_id: str = ""
    summary: str = ""
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    related_object_type: str = ""
    related_object_id: str = ""
    parent_event_id: str = ""
    causal_root_id: str = ""
    created_at: str = ""


class BandCheckpointIssueInfo(BaseModel):
    code: str = ""
    severity: str = "info"
    category: OverClosureRiskCategory = ""
    issue_group: IssueGroup = ""
    description: str = ""
    detail: str = ""


class BandCheckpointDetail(BaseModel):
    id: str = ""
    project_id: str = ""
    arc_id: str = ""
    band_id: str = ""
    chapter_start: int = 0
    chapter_end: int = 0
    trigger_source: str = ""
    boundary_kind: str = ""
    boundary_chapter: int = 0
    status: CheckpointStatus = "pending"
    summary: str = ""
    reason: str = ""
    issues: list[BandCheckpointIssueInfo] = Field(default_factory=list)
    decision_refs: list[DecisionEventInfo] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    resolved_at: str = ""


class NextBandSummary(BaseModel):
    band_id: str = ""
    chapter_start: int = 0
    chapter_end: int = 0
    chapter_titles: list[str] = Field(default_factory=list)
    band_task_contract: list[PlanTaskItem] = Field(default_factory=list)


def new_project_governance(
    *,
    default_operation_mode: str = "blackbox",
    review_interval_chapters: int = 0,
) -> ProjectGovernanceSettings:
    return ProjectGovernanceSettings(
        default_operation_mode=str(default_operation_mode or "blackbox").strip() or "blackbox",
        review_interval_chapters=max(0, int(review_interval_chapters or 0)),
        progression_mode="serial_canon_band_guard",
        auto_band_checkpoint=True,
        band_warn_action="pause",
        manual_checkpoints_enabled=True,
        future_constraints_enabled=True,
    )


def legacy_project_governance(
    *,
    default_operation_mode: str = "blackbox",
    review_interval_chapters: int = 0,
) -> ProjectGovernanceSettings:
    return ProjectGovernanceSettings(
        default_operation_mode=str(default_operation_mode or "blackbox").strip() or "blackbox",
        review_interval_chapters=max(0, int(review_interval_chapters or 0)),
        progression_mode="legacy_relaxed",
        auto_band_checkpoint=False,
        band_warn_action="pause",
        manual_checkpoints_enabled=False,
        future_constraints_enabled=False,
    )


def normalize_project_governance(
    raw: str | dict[str, Any] | None,
    *,
    fallback_operation_mode: str = "blackbox",
    fallback_review_interval: int = 0,
    treat_empty_as_legacy: bool = True,
) -> ProjectGovernanceSettings:
    payload: dict[str, Any]
    if isinstance(raw, dict):
        payload = dict(raw)
    else:
        try:
            payload = json.loads(raw or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
    if not payload and treat_empty_as_legacy:
        return legacy_project_governance(
            default_operation_mode=fallback_operation_mode,
            review_interval_chapters=fallback_review_interval,
        )
    merged = {
        "default_operation_mode": fallback_operation_mode,
        "review_interval_chapters": fallback_review_interval,
        **payload,
    }
    merged["default_operation_mode"] = (
        str(merged.get("default_operation_mode", fallback_operation_mode) or "blackbox").strip()
        or "blackbox"
    )
    try:
        merged["review_interval_chapters"] = max(0, int(merged.get("review_interval_chapters", fallback_review_interval) or 0))
    except (TypeError, ValueError):
        merged["review_interval_chapters"] = max(0, int(fallback_review_interval or 0))
    return ProjectGovernanceSettings.model_validate(merged)


def load_plan_task_contract(raw: str | list[dict[str, Any]] | None) -> list[PlanTaskItem]:
    if isinstance(raw, list):
        payload = raw
    else:
        try:
            payload = json.loads(raw or "[]") or []
        except (json.JSONDecodeError, TypeError):
            payload = []
    tasks: list[PlanTaskItem] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            tasks.append(PlanTaskItem.model_validate(item))
        except Exception:
            continue
    return tasks


def derive_chapter_task_contract(goals: list[str]) -> list[PlanTaskItem]:
    tasks: list[PlanTaskItem] = []
    for goal in goals[:4]:
        text = str(goal or "").strip()
        if not text:
            continue
        tasks.append(
            PlanTaskItem(
                task_type="plot_advance",
                description=text,
                source="derived_from_goals",
            )
        )
    return tasks


def derive_band_task_contract(schedule: "BandDelightSchedule") -> list[PlanTaskItem]:
    tasks: list[PlanTaskItem] = []
    seen_reward_targets: set[str] = set()
    for reward in schedule.scheduled_rewards:
        target = str(reward.category or "").strip()
        if not target or target in seen_reward_targets:
            continue
        seen_reward_targets.add(target)
        tasks.append(
            PlanTaskItem(
                task_type="experience_delivery",
                description=f"本 band 至少交付一次 {target} 回报。",
                target_name=target,
                source="derived_from_schedule",
            )
        )
    for beat in schedule.curiosity_beats[:2]:
        if not str(beat.question_open or "").strip():
            continue
        tasks.append(
            PlanTaskItem(
                task_type="setup",
                description=str(beat.question_open or "").strip(),
                source="derived_from_schedule",
            )
        )
        if str(beat.question_resolve or "").strip():
            tasks.append(
                PlanTaskItem(
                    task_type="plot_advance",
                    description=str(beat.question_resolve or "").strip(),
                    source="derived_from_schedule",
                )
            )
    return tasks


def governance_to_json(settings: ProjectGovernanceSettings) -> str:
    return json.dumps(settings.model_dump(mode="json"), ensure_ascii=False)


def ensure_decision_event_type(value: str) -> str:
    event_type = str(value or "").strip()
    if event_type not in KNOWN_DECISION_EVENT_TYPES:
        raise ValueError(f"未知 DecisionEvent.event_type: {event_type or '<empty>'}")
    return event_type


_FACT_CONFLICT_HINTS = {
    "continuity",
    "future_constraint",
    "next_band_compatibility",
    "timeline",
    "state",
    "state_conflict",
    "character",
    "relationship",
    "relation",
    "intra_band_consistency",
}
_DIRECTOR_IMBALANCE_HINTS = {
    "director_imbalance",
    "plan_task_fulfillment",
    "chapter_task_contract",
    "band_task_completion",
    "future_resource_preservation",
    "payoff",
    "pacing",
    "experience",
    "experience_delivery",
    "stall",
    "immersion",
}
_RUNTIME_HINTS = {
    "runtime",
    "llm",
    "stage",
    "memory",
    "fallback",
    "timeout",
    "retry",
}
_GOVERNANCE_ACTION_HINTS = {
    "governance",
    "manual",
    "override",
    "approve",
    "checkpoint_action",
    "constraint_update",
}


def issue_group_for_issue(*, issue_type: str = "", rule_name: str = "", code: str = "") -> IssueGroup:
    text = " ".join(str(part or "") for part in (issue_type, rule_name, code)).lower()
    if not text.strip():
        return ""
    if any(hint in text for hint in _RUNTIME_HINTS):
        return "runtime_observation"
    if any(hint in text for hint in _GOVERNANCE_ACTION_HINTS):
        return "governance_action"
    if any(hint in text for hint in _DIRECTOR_IMBALANCE_HINTS):
        return "director_imbalance"
    if any(hint in text for hint in _FACT_CONFLICT_HINTS):
        return "fact_conflict"
    return "fact_conflict"


def plan_task_contract_to_json(tasks: list[PlanTaskItem]) -> str:
    return json.dumps([item.model_dump(mode="json") for item in tasks], ensure_ascii=False)


def band_is_first_chapter(band_start: int, chapter_number: int) -> bool:
    return int(chapter_number or 0) == int(band_start or 0)


def chapter_blocking_message(reason: BlockingReasonCode, *, chapter_number: int = 0, band_id: str = "") -> str:
    if reason == "chapter_not_canon":
        return f"前序章节尚未进入 canon，暂不能开启第{chapter_number}章。"
    if reason == "band_checkpoint_pending":
        return f"{band_id or '上一 band'} 尚未完成 checkpoint 放行。"
    if reason == "band_checkpoint_warn":
        return f"{band_id or '上一 band'} checkpoint 出现警告，需人工确认后继续。"
    if reason == "band_checkpoint_fail":
        return f"{band_id or '上一 band'} checkpoint 未通过，需修复或 override 后继续。"
    if reason == "future_constraint_block":
        return "存在未来叙事约束冲突，需先处理后才能继续。"
    return ""
