from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forwin.state.updater import StateUpdater
from forwin.generation.pipeline_core.run_control import _bind_pipeline_runtime_hooks, run, run_existing_project, _emit_progress, _bind_governance_runtime, _clear_governance_runtime, _start_governance_stage_span, _finish_governance_stage_span, _record_stage_transition, _latest_provisional_gate_snapshot, _new_failed_provisional_gate, _block_on_scenario_rehearsal, _block_on_provisional_failure, _pending_chapter_numbers_for_active_arc, _materialize_next_genesis_arc_if_needed, continue_project
from forwin.generation.pipeline_core.acceptance import accept_review
from forwin.generation.pipeline_core.governance import _project_policy, _record_decision_event, _record_rule_decision_event, _audit_current_plan_before_write, _audit_future_plans_after_acceptance, _record_future_plan_audit_events, _record_generation_audit_checkpoint_if_due, _generation_audit_checkpoint_payload, _previous_band_row, _manual_boundary_checkpoint, _strict_progression_block, _create_auto_band_checkpoint
from forwin.generation.pipeline_core.runtime_helpers import _make_state_helpers, _select_skill_layers, _filter_supported_kwargs, _call_with_compatible_kwargs, _save_prompt_trace_payload, _record_prompt_trace_performance_spans
from forwin.generation.pipeline_core.review_autofix import _persist_draft_and_review, _review_current_output, _plan_writer_output_entities, _apply_canon_name_drift_autofix, _apply_placeholder_leakage_autofix, _project_character_names, _replace_canon_name_strings, _review_event_payload, _review_issue_payloads, _record_map_movement_review_issues, _review_canon_risk, _load_json_list, _chapter_plan_snapshot, _band_plan_snapshot, _repair_verification_issue, _review_with_repair_verification, _repair_policy_requested_scope, _review_has_structural_repair_issue
from forwin.generation.pipeline_core.repair_patches import (
    _arc_payoff_patch_payload,
    _band_schedule_patch_payload,
    _chapter_experience_patch_payload,
    _countdown_repair_rule_anchors,
    _current_chapter_repair_experience_plan,
    _reader_promise_from_row,
    _replace_band_schedule,
    _structure_data_from_row,
)
from forwin.generation.pipeline_core.gate_delegation import (
    _resolve_checkpoint_gate,
    _resolve_gate_delegation,
)
from forwin.generation.pipeline_core.project_chapters import _run_project_chapters
from forwin.generation.pipeline_core.writer_attention import _write_chapter_with_attention_fallback
from forwin.generation.pipeline_core.quality_gates import _is_timeout_like, _is_transient_llm_like, _transient_retry_delay, _current_model_identity, _audit_operation_id, _drain_llm_attempt_events, _safe_prompt_trace_attempts, _error_category_from_attempts, _diagnostic_kind_for_failure, _record_failure_prompt_trace, _record_model_fallback_payloads
from forwin.generation.pipeline_core.world_projection import _prompt_trace_success_summary, _run_phase3_pass
from forwin.generation.pipeline_core.finalization import _flush_background_llm_trace, _run_provisional_band_preview, _abort_requested, _pause_requested, _paused_result, _cancelled_result, _normalize_provisional_verdict, _should_degrade_provisional_preview, _build_provisional_fallback, _load_writer_output_from_meta, _load_review_verdict, _seed_state


class ChapterPipeline:
    def __init__(
        self,
        *,
        infrastructure: Any,
        policy: Any,
        engine: Any,
        session_factory: Any,
        llm_client: Any,
        skill_registry: Any,
        skill_router: Any,
        skill_prompt_layer_builder: Any,
        arc_director: Any,
        book_genesis: Any,
        subworld_manager: Any,
        retrieval_broker: Any,
        artifact_store: Any,
        observability: Any,
        writer: Any,
        provisional_writer: Any,
        stage_analyzer: Any,
        pacing_strategist: Any,
        replan_governor: Any,
        npc_intent_generator: Any,
        world_simulator: Any,
        arc_envelope_manager: Any,
        draft_review: Any,
        repair: Any,
        repair_verifier: Any,
        canon_preparation: Any,
        canon_admission: Any,
        gate_delegation: Any,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        task_id: str = "",
        root_event_id: str = "",
    ) -> None:
        self.infrastructure = infrastructure
        self.policy = policy
        self.progress_callback = progress_callback
        self.should_abort = should_abort
        self.should_pause = should_pause
        self._governance_task_id = str(task_id or "").strip()
        self._governance_root_event_id = str(root_event_id or "").strip()
        self._governance_runtime_project_id = ""
        self._governance_runtime_updater: StateUpdater | None = None
        self._governance_stage_name = ""
        self._governance_stage_started_at = 0.0
        self._governance_stage_chapter_number = 0
        self._governance_stage_span: Any | None = None

        self.engine = engine
        self._SessionFactory = session_factory
        self.llm_client = llm_client
        self.skill_registry = skill_registry
        self.skill_router = skill_router
        self.skill_prompt_layer_builder = skill_prompt_layer_builder
        self.arc_director = arc_director
        self.book_genesis = book_genesis
        self.subworld_manager = subworld_manager
        self.retrieval_broker = retrieval_broker
        self.artifact_store = artifact_store
        self.observability = observability
        self.writer = writer
        self.provisional_writer = provisional_writer
        self.stage_analyzer = stage_analyzer
        self.pacing_strategist = pacing_strategist
        self.replan_governor = replan_governor
        self.npc_intent_generator = npc_intent_generator
        self.world_simulator = world_simulator
        self.arc_envelope_manager = arc_envelope_manager
        self.draft_review = draft_review
        self.repair = repair
        self.repair_verifier = repair_verifier
        self.canon_preparation = canon_preparation
        self.canon_admission = canon_admission
        self.gate_delegation = gate_delegation
        self._bind_pipeline_runtime_hooks()

    _bind_pipeline_runtime_hooks = _bind_pipeline_runtime_hooks
    run = run
    run_existing_project = run_existing_project
    _emit_progress = _emit_progress
    _bind_governance_runtime = _bind_governance_runtime
    _clear_governance_runtime = _clear_governance_runtime
    _start_governance_stage_span = _start_governance_stage_span
    _finish_governance_stage_span = _finish_governance_stage_span
    _record_stage_transition = _record_stage_transition
    _latest_provisional_gate_snapshot = _latest_provisional_gate_snapshot
    _new_failed_provisional_gate = _new_failed_provisional_gate
    _block_on_scenario_rehearsal = _block_on_scenario_rehearsal
    _block_on_provisional_failure = _block_on_provisional_failure
    _pending_chapter_numbers_for_active_arc = _pending_chapter_numbers_for_active_arc
    _materialize_next_genesis_arc_if_needed = _materialize_next_genesis_arc_if_needed
    continue_project = continue_project
    accept_review = accept_review
    _project_policy = _project_policy
    _record_decision_event = _record_decision_event
    _record_rule_decision_event = _record_rule_decision_event
    _resolve_gate_delegation = _resolve_gate_delegation
    _resolve_checkpoint_gate = _resolve_checkpoint_gate
    _audit_current_plan_before_write = _audit_current_plan_before_write
    _audit_future_plans_after_acceptance = _audit_future_plans_after_acceptance
    _record_future_plan_audit_events = _record_future_plan_audit_events
    _record_generation_audit_checkpoint_if_due = _record_generation_audit_checkpoint_if_due
    _generation_audit_checkpoint_payload = _generation_audit_checkpoint_payload
    _previous_band_row = _previous_band_row
    _manual_boundary_checkpoint = _manual_boundary_checkpoint
    _strict_progression_block = _strict_progression_block
    _create_auto_band_checkpoint = _create_auto_band_checkpoint
    _make_state_helpers = _make_state_helpers
    _select_skill_layers = _select_skill_layers
    _filter_supported_kwargs = _filter_supported_kwargs
    _call_with_compatible_kwargs = _call_with_compatible_kwargs
    _save_prompt_trace_payload = _save_prompt_trace_payload
    _record_prompt_trace_performance_spans = _record_prompt_trace_performance_spans
    _persist_draft_and_review = _persist_draft_and_review
    _review_current_output = _review_current_output
    _plan_writer_output_entities = _plan_writer_output_entities
    _apply_canon_name_drift_autofix = _apply_canon_name_drift_autofix
    _apply_placeholder_leakage_autofix = _apply_placeholder_leakage_autofix
    _project_character_names = _project_character_names
    _replace_canon_name_strings = _replace_canon_name_strings
    _review_event_payload = _review_event_payload
    _review_issue_payloads = _review_issue_payloads
    _record_map_movement_review_issues = _record_map_movement_review_issues
    _review_canon_risk = _review_canon_risk
    _load_json_list = _load_json_list
    _chapter_plan_snapshot = _chapter_plan_snapshot
    _band_plan_snapshot = _band_plan_snapshot
    _repair_verification_issue = _repair_verification_issue
    _review_with_repair_verification = _review_with_repair_verification
    _repair_policy_requested_scope = _repair_policy_requested_scope
    _review_has_structural_repair_issue = _review_has_structural_repair_issue
    _replace_band_schedule = _replace_band_schedule
    _structure_data_from_row = _structure_data_from_row
    _reader_promise_from_row = _reader_promise_from_row
    _current_chapter_repair_experience_plan = _current_chapter_repair_experience_plan
    _chapter_experience_patch_payload = _chapter_experience_patch_payload
    _countdown_repair_rule_anchors = _countdown_repair_rule_anchors
    _band_schedule_patch_payload = _band_schedule_patch_payload
    _arc_payoff_patch_payload = _arc_payoff_patch_payload
    _run_project_chapters = _run_project_chapters
    _write_chapter_with_attention_fallback = _write_chapter_with_attention_fallback
    _is_timeout_like = _is_timeout_like
    _is_transient_llm_like = _is_transient_llm_like
    _transient_retry_delay = _transient_retry_delay
    _current_model_identity = _current_model_identity
    _audit_operation_id = _audit_operation_id
    _drain_llm_attempt_events = _drain_llm_attempt_events
    _safe_prompt_trace_attempts = _safe_prompt_trace_attempts
    _error_category_from_attempts = _error_category_from_attempts
    _diagnostic_kind_for_failure = _diagnostic_kind_for_failure
    _record_failure_prompt_trace = _record_failure_prompt_trace
    _record_model_fallback_payloads = _record_model_fallback_payloads
    _prompt_trace_success_summary = _prompt_trace_success_summary
    _run_phase3_pass = _run_phase3_pass
    _flush_background_llm_trace = _flush_background_llm_trace
    _run_provisional_band_preview = _run_provisional_band_preview
    _abort_requested = _abort_requested
    _pause_requested = _pause_requested
    _paused_result = _paused_result
    _cancelled_result = _cancelled_result
    _normalize_provisional_verdict = _normalize_provisional_verdict
    _should_degrade_provisional_preview = _should_degrade_provisional_preview
    _build_provisional_fallback = _build_provisional_fallback
    _load_writer_output_from_meta = _load_writer_output_from_meta
    _load_review_verdict = _load_review_verdict
    _seed_state = _seed_state


__all__ = ["ChapterPipeline"]
