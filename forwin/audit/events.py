from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


DecisionEventFamily = Literal[
    "business_event",
    "audit_action",
    "runtime_observation",
    "evaluation_verdict",
]


DecisionActorType = Literal["system", "scheduler", "manual_ui", "api", "extension", "worker"]


class DecisionEventType:
    GENERATION_REQUESTED = "generation_requested"
    CONTINUE_REQUESTED = "continue_requested"
    AUTO_CONTINUE_DECISION = "auto_continue_decision"
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

    RUNTIME_POLICY_UPDATED = "runtime_policy_updated"
    MANUAL_CHECKPOINT_CREATED = "manual_checkpoint_created"
    MANUAL_CHECKPOINT_HIT = "manual_checkpoint_hit"
    CONSTRAINT_CREATED = "constraint_created"
    CONSTRAINT_UPDATED = "constraint_updated"
    CONSTRAINT_ARCHIVED = "constraint_archived"
    PLAN_TASK_CONTRACT_UPDATED = "plan_task_contract_updated"
    FUTURE_PLAN_AUDIT_RUN = "future_plan_audit_run"
    FUTURE_PLAN_PATCH_APPLIED = "future_plan_patch_applied"
    GENERATION_AUDIT_CHECKPOINT_REACHED = "generation_audit_checkpoint_reached"

    PAUSE_REQUESTED = "pause_requested"
    PAUSE_REACHED = "pause_reached"
    TERMINATE_REQUESTED = "terminate_requested"
    TERMINATE_REACHED = "terminate_reached"

    REVIEW_VERDICT_RECORDED = "review_verdict_recorded"
    RULE_DECISION_EVALUATED = "rule_decision_evaluated"
    REPAIR_STARTED = "repair_started"
    REPAIR_FAILED = "repair_failed"
    REPAIR_SUCCEEDED = "repair_succeeded"
    REPAIR_BODY_OVER_BUDGET = "repair_body_over_budget"
    REPAIR_COMPRESSION_APPLIED = "repair_compression_applied"
    REPAIR_NEEDS_HUMAN_COMPRESSION = "repair_needs_human_compression"
    REVIEW_APPROVED = "review_approved"
    FORCED_ACCEPT_APPLIED = "forced_accept_applied"
    GATE_DELEGATION_REQUESTED = "gate_delegation_requested"
    GATE_DELEGATION_DECIDED = "gate_delegation_decided"
    GATE_DELEGATION_FAILED = "gate_delegation_failed"
    GATE_DELEGATION_APPROVED = "gate_delegation_approved"

    BAND_CHECKPOINT_CREATED = "band_checkpoint_created"
    BAND_CHECKPOINT_HIT = "band_checkpoint_hit"
    CHECKPOINT_EVALUATOR_ERROR = "checkpoint_evaluator_error"
    BAND_CHECKPOINT_APPROVED = "band_checkpoint_approved"
    BAND_CHECKPOINT_OVERRIDDEN = "band_checkpoint_overridden"

    CANON_COMMIT = "canon_commit"
    CANON_COMMIT_FAILED = "canon_commit_failed"
    CANON_COMMIT_BLOCKED = "canon_commit_blocked"
    HARD_GATE_HIT = "hard_gate_hit"
    PULP_BEAT_EVALUATED = "pulp_beat_evaluated"

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
    DEFERRED_MAINTENANCE_RECORDED = "deferred_maintenance_recorded"
    MAP_GENERATION_STARTED = "map_generation_started"
    MAP_GENERATION_SUCCEEDED = "map_generation_succeeded"
    MAP_GENERATION_FAILED = "map_generation_failed"
    MAP_EXPANSION_STARTED = "map_expansion_started"
    MAP_EXPANSION_SUCCEEDED = "map_expansion_succeeded"
    MAP_EXPANSION_FAILED = "map_expansion_failed"
    ARC_ACTIVATION_REVIEW_PACK_BUILT = "arc_activation_review_pack_built"
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
    CHARACTER_CREATED = "character_created"
    CHARACTER_MERGED_EXISTING = "character_merged_existing"
    CHARACTER_ROSTER_MATERIALIZED = "character_roster_materialized"
    PERSONALITY_LOADOUT_UPDATED = "personality_loadout_updated"
    PERSONALITY_LOADOUT_AUTO_ASSIGNED = "personality_loadout_auto_assigned"
    PERSONALITY_LOADOUT_REASSIGNED = "personality_loadout_reassigned"
    PERSONALITY_LOADOUT_MANUAL_OVERRIDE = "personality_loadout_manual_override"
    PERSONALITY_ASSIGNMENT_BACKFILL_COMPLETED = "personality_assignment_backfill_completed"
    PERSONALITY_RELATIONSHIP_ENRICHED = "personality_relationship_enriched"
    ENTITY_REGISTERED = "entity_registered"
    ENTITY_ALIAS_REGISTERED = "entity_alias_registered"
    ENTITY_BACKGROUND_GENERIC = "entity_background_generic"
    ENTITY_PLAN_CONFLICT = "entity_plan_conflict"
    ENTITY_ALIAS_CONFLICT = "entity_alias_conflict"
    CHARACTER_INTEGRITY_CHECK_FAILED = "character_integrity_check_failed"
    TASK_OPERATION_STARTED = "task_operation_started"
    TASK_OPERATION_SUCCEEDED = "task_operation_succeeded"
    TASK_OPERATION_FAILED = "task_operation_failed"
    TASK_CLEANUP_STARTED = "task_cleanup_started"
    TASK_CLEANUP_FINISHED = "task_cleanup_finished"
    GENERATION_WORKER_CLAIMED = "generation_worker_claimed"
    GENERATION_WORKER_RECLAIMED = "generation_worker_reclaimed"
    GENERATION_WORKER_HEARTBEAT_FAILED = "generation_worker_heartbeat_failed"
    GENERATION_WORKER_EXECUTION_FAILED = "generation_worker_execution_failed"
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
    DecisionEventType.AUTO_CONTINUE_DECISION,
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
    DecisionEventType.RUNTIME_POLICY_UPDATED,
    DecisionEventType.MANUAL_CHECKPOINT_CREATED,
    DecisionEventType.MANUAL_CHECKPOINT_HIT,
    DecisionEventType.CONSTRAINT_CREATED,
    DecisionEventType.CONSTRAINT_UPDATED,
    DecisionEventType.CONSTRAINT_ARCHIVED,
    DecisionEventType.PLAN_TASK_CONTRACT_UPDATED,
    DecisionEventType.FUTURE_PLAN_AUDIT_RUN,
    DecisionEventType.FUTURE_PLAN_PATCH_APPLIED,
    DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
    DecisionEventType.PAUSE_REQUESTED,
    DecisionEventType.PAUSE_REACHED,
    DecisionEventType.TERMINATE_REQUESTED,
    DecisionEventType.TERMINATE_REACHED,
    DecisionEventType.REVIEW_VERDICT_RECORDED,
    DecisionEventType.RULE_DECISION_EVALUATED,
    DecisionEventType.REPAIR_STARTED,
    DecisionEventType.REPAIR_FAILED,
    DecisionEventType.REPAIR_SUCCEEDED,
    DecisionEventType.REPAIR_BODY_OVER_BUDGET,
    DecisionEventType.REPAIR_COMPRESSION_APPLIED,
    DecisionEventType.REPAIR_NEEDS_HUMAN_COMPRESSION,
    DecisionEventType.REVIEW_APPROVED,
    DecisionEventType.FORCED_ACCEPT_APPLIED,
    DecisionEventType.GATE_DELEGATION_REQUESTED,
    DecisionEventType.GATE_DELEGATION_DECIDED,
    DecisionEventType.GATE_DELEGATION_FAILED,
    DecisionEventType.GATE_DELEGATION_APPROVED,
    DecisionEventType.BAND_CHECKPOINT_CREATED,
    DecisionEventType.BAND_CHECKPOINT_HIT,
    DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
    DecisionEventType.BAND_CHECKPOINT_APPROVED,
    DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN,
    DecisionEventType.CANON_COMMIT,
    DecisionEventType.CANON_COMMIT_BLOCKED,
    DecisionEventType.CANON_COMMIT_FAILED,
    DecisionEventType.HARD_GATE_HIT,
    DecisionEventType.PULP_BEAT_EVALUATED,
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
    DecisionEventType.DEFERRED_MAINTENANCE_RECORDED,
    DecisionEventType.MAP_GENERATION_STARTED,
    DecisionEventType.MAP_GENERATION_SUCCEEDED,
    DecisionEventType.MAP_GENERATION_FAILED,
    DecisionEventType.MAP_EXPANSION_STARTED,
    DecisionEventType.MAP_EXPANSION_SUCCEEDED,
    DecisionEventType.MAP_EXPANSION_FAILED,
    DecisionEventType.ARC_ACTIVATION_REVIEW_PACK_BUILT,
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
    DecisionEventType.ENTITY_REGISTERED,
    DecisionEventType.ENTITY_ALIAS_REGISTERED,
    DecisionEventType.ENTITY_BACKGROUND_GENERIC,
    DecisionEventType.ENTITY_PLAN_CONFLICT,
    DecisionEventType.ENTITY_ALIAS_CONFLICT,
    DecisionEventType.TASK_OPERATION_STARTED,
    DecisionEventType.TASK_OPERATION_SUCCEEDED,
    DecisionEventType.TASK_OPERATION_FAILED,
    DecisionEventType.TASK_CLEANUP_STARTED,
    DecisionEventType.TASK_CLEANUP_FINISHED,
    DecisionEventType.GENERATION_WORKER_CLAIMED,
    DecisionEventType.GENERATION_WORKER_RECLAIMED,
    DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED,
    DecisionEventType.GENERATION_WORKER_EXECUTION_FAILED,
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


def ensure_decision_event_type(value: str) -> str:
    event_type = str(value or "").strip()
    if event_type not in KNOWN_DECISION_EVENT_TYPES:
        raise ValueError(f"未知 DecisionEvent.event_type: {event_type or '<empty>'}")
    return event_type


__all__ = [
    "DecisionActorType",
    "DecisionEventFamily",
    "DecisionEventInfo",
    "DecisionEventType",
    "KNOWN_DECISION_EVENT_TYPES",
    "ensure_decision_event_type",
]
