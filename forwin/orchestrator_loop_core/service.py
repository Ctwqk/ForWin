from __future__ import annotations

from forwin.orchestrator_loop_core.common import *
from forwin.orchestrator_loop_core import acceptance as _acceptance_module
from forwin.orchestrator_loop_core import finalization as _finalization_module
from forwin.orchestrator_loop_core import governance as _governance_module
from forwin.orchestrator_loop_core import project_chapters as _project_chapters_module
from forwin.orchestrator_loop_core import quality_gates as _quality_gates_module
from forwin.orchestrator_loop_core import repair_loop as _repair_loop_module
from forwin.orchestrator_loop_core import review_autofix as _review_autofix_module
from forwin.orchestrator_loop_core import run_control as _run_control_module
from forwin.orchestrator_loop_core import runtime_helpers as _runtime_helpers_module
from forwin.orchestrator_loop_core import world_projection as _world_projection_module
from forwin.orchestrator_loop_core import writer_attention as _writer_attention_module
from forwin.orchestrator_loop_core.run_control import _bind_orchestrator_runtime_hooks, run, run_existing_project, _emit_progress, _bind_governance_runtime, _clear_governance_runtime, _start_governance_stage_span, _finish_governance_stage_span, _record_stage_transition, _latest_provisional_gate_snapshot, _new_failed_provisional_gate, _block_on_scenario_rehearsal, _block_on_provisional_failure, _pending_chapter_numbers_for_active_arc, _materialize_next_genesis_arc_if_needed, continue_project
from forwin.orchestrator_loop_core.acceptance import accept_review
from forwin.orchestrator_loop_core.governance import _project_governance, _record_decision_event, _record_engine_decision_event, _audit_current_plan_before_write, _audit_future_plans_after_acceptance, _future_plan_audit_plans, _future_plan_audit_band_rows, _record_future_plan_audit_events, _record_generation_audit_checkpoint_if_due, _generation_audit_checkpoint_payload, _previous_band_row, _manual_boundary_checkpoint, _strict_progression_block, _create_auto_band_checkpoint, _filter_supported_state_changes
from forwin.orchestrator_loop_core.runtime_helpers import _make_state_helpers, _select_skill_layers, _filter_supported_kwargs, _call_with_compatible_kwargs, _save_prompt_trace_payload, _record_prompt_trace_performance_spans
from forwin.orchestrator_loop_core.review_autofix import _persist_draft_and_review, _review_current_output, _apply_canon_name_drift_autofix, _apply_subworld_admission_autofix, _apply_placeholder_leakage_autofix, _placeholder_role_replacement, _looks_like_genericizable_unknown_reference, _project_character_names, _generic_subworld_reference, _subworld_role_titles, _replace_canon_name_strings, _review_event_payload, _review_issue_payloads, _record_map_movement_review_issues, _review_canon_risk, _load_json_list, _chapter_plan_snapshot, _band_plan_snapshot, _repair_verification_issue, _review_with_repair_verification, _repair_policy_requested_scope, _review_has_structural_repair_issue
from forwin.orchestrator_loop_core.repair_loop import _review_and_maybe_rewrite, _review_meta_json, _default_repair_instruction, _apply_repair_patch, _replace_band_schedule, _structure_data_from_row, _reader_promise_from_row, _current_chapter_repair_experience_plan, _chapter_experience_patch_payload, _countdown_repair_rule_anchors, _band_schedule_patch_payload, _arc_payoff_patch_payload
from forwin.orchestrator_loop_core.project_chapters import _run_project_chapters
from forwin.orchestrator_loop_core.writer_attention import _write_chapter_with_attention_fallback
from forwin.orchestrator_loop_core.quality_gates import _is_timeout_like, _is_transient_llm_like, _transient_retry_delay, _current_model_identity, _audit_operation_id, _drain_llm_attempt_events, _safe_prompt_trace_attempts, _error_category_from_attempts, _diagnostic_kind_for_failure, _record_failure_prompt_trace, _record_model_fallback_payloads, _apply_canon_quality_gate, _run_obligation_form_gate, _prepare_deferred_acceptance_if_needed, _band_scope_candidates, _band_row_by_id, _latest_draft_and_review_for_chapter, _apply_canon_candidate
from forwin.orchestrator_loop_core.world_projection import _prompt_trace_success_summary, _apply_world_v4_gate, _filter_resolvable_events, _filter_resolvable_state_changes, _ensure_genesis_canon_seed_entities, _collect_subworld_candidate_names, _validate_subworld_admission, _run_phase3_pass
from forwin.orchestrator_loop_core.finalization import _flush_background_llm_trace, _compile_world_model_after_acceptance, _run_provisional_band_preview, _abort_requested, _pause_requested, _paused_result, _cancelled_result, _normalize_provisional_verdict, _should_degrade_provisional_preview, _build_provisional_fallback, _load_writer_output_from_meta, _load_review_verdict, _seed_state


class WritingOrchestrator:
    def __init__(
        self,
        config: Config | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        *,
        services: RuntimeServices | None = None,
    ) -> None:
        if services is None:
            container_cls = globals().get("RuntimeContainer")
            if container_cls is None:
                from forwin.runtime.container import RuntimeContainer as container_cls

            services = container_cls.from_config(config or Config.from_env()).services()
        self.services = services
        self.config = services.config
        self.progress_callback = progress_callback
        self.should_abort = should_abort
        self.should_pause = should_pause
        self._governance_task_id = str(getattr(self.config, "governance_task_id", "") or "").strip()
        self._governance_root_event_id = str(
            getattr(self.config, "governance_causal_root_id", "") or ""
        ).strip()
        self._governance_runtime_project_id = ""
        self._governance_runtime_updater: StateUpdater | None = None
        self._governance_stage_name = ""
        self._governance_stage_started_at = 0.0
        self._governance_stage_chapter_number = 0
        self._governance_stage_span: Any | None = None

        self.engine = services.engine
        self._SessionFactory = services.session_factory
        self.llm_client = services.llm_client
        self.skill_registry = services.skill_runtime.registry
        self.skill_router = services.skill_runtime.router
        self.skill_prompt_layer_builder = services.skill_runtime.prompt_layer_builder
        self.arc_director = services.arc_director
        self.book_genesis = services.book_genesis
        self.subworld_manager = services.subworld_manager
        self.retrieval_broker = services.retrieval_broker
        self.artifact_store = services.artifact_store
        self.observability = getattr(services, "observability", NullObservability())
        self.writer = services.writer
        self.provisional_writer = services.provisional_writer
        self.stage_analyzer = services.stage_analyzer
        self.pacing_strategist = services.pacing_strategist
        self.replan_governor = services.replan_governor
        self.npc_intent_generator = services.npc_intent_generator
        self.world_simulator = services.world_simulator
        self.arc_envelope_manager = services.arc_envelope_manager
        self.review_hub = services.review_hub
        self.repair_verifier = services.repair_verifier
        self._bind_orchestrator_runtime_hooks()



WritingOrchestrator._bind_orchestrator_runtime_hooks = _bind_orchestrator_runtime_hooks
WritingOrchestrator.run = run
WritingOrchestrator.run_existing_project = run_existing_project
WritingOrchestrator._emit_progress = _emit_progress
WritingOrchestrator._bind_governance_runtime = _bind_governance_runtime
WritingOrchestrator._clear_governance_runtime = _clear_governance_runtime
WritingOrchestrator._start_governance_stage_span = _start_governance_stage_span
WritingOrchestrator._finish_governance_stage_span = _finish_governance_stage_span
WritingOrchestrator._record_stage_transition = _record_stage_transition
WritingOrchestrator._latest_provisional_gate_snapshot = _latest_provisional_gate_snapshot
WritingOrchestrator._new_failed_provisional_gate = _new_failed_provisional_gate
WritingOrchestrator._block_on_scenario_rehearsal = _block_on_scenario_rehearsal
WritingOrchestrator._block_on_provisional_failure = _block_on_provisional_failure
WritingOrchestrator._pending_chapter_numbers_for_active_arc = _pending_chapter_numbers_for_active_arc
WritingOrchestrator._materialize_next_genesis_arc_if_needed = _materialize_next_genesis_arc_if_needed
WritingOrchestrator.continue_project = continue_project
WritingOrchestrator.accept_review = accept_review
WritingOrchestrator._project_governance = _project_governance
WritingOrchestrator._record_decision_event = _record_decision_event
WritingOrchestrator._record_engine_decision_event = _record_engine_decision_event
WritingOrchestrator._audit_current_plan_before_write = _audit_current_plan_before_write
WritingOrchestrator._audit_future_plans_after_acceptance = _audit_future_plans_after_acceptance
WritingOrchestrator._future_plan_audit_plans = _future_plan_audit_plans
WritingOrchestrator._future_plan_audit_band_rows = _future_plan_audit_band_rows
WritingOrchestrator._record_future_plan_audit_events = _record_future_plan_audit_events
WritingOrchestrator._record_generation_audit_checkpoint_if_due = _record_generation_audit_checkpoint_if_due
WritingOrchestrator._generation_audit_checkpoint_payload = _generation_audit_checkpoint_payload
WritingOrchestrator._previous_band_row = _previous_band_row
WritingOrchestrator._manual_boundary_checkpoint = _manual_boundary_checkpoint
WritingOrchestrator._strict_progression_block = _strict_progression_block
WritingOrchestrator._create_auto_band_checkpoint = _create_auto_band_checkpoint
WritingOrchestrator._filter_supported_state_changes = _filter_supported_state_changes
WritingOrchestrator._make_state_helpers = _make_state_helpers
WritingOrchestrator._select_skill_layers = _select_skill_layers
WritingOrchestrator._filter_supported_kwargs = _filter_supported_kwargs
WritingOrchestrator._call_with_compatible_kwargs = _call_with_compatible_kwargs
WritingOrchestrator._save_prompt_trace_payload = _save_prompt_trace_payload
WritingOrchestrator._record_prompt_trace_performance_spans = _record_prompt_trace_performance_spans
WritingOrchestrator._persist_draft_and_review = _persist_draft_and_review
WritingOrchestrator._review_current_output = _review_current_output
WritingOrchestrator._apply_canon_name_drift_autofix = _apply_canon_name_drift_autofix
WritingOrchestrator._apply_subworld_admission_autofix = _apply_subworld_admission_autofix
WritingOrchestrator._apply_placeholder_leakage_autofix = _apply_placeholder_leakage_autofix
WritingOrchestrator._placeholder_role_replacement = _placeholder_role_replacement
WritingOrchestrator._looks_like_genericizable_unknown_reference = _looks_like_genericizable_unknown_reference
WritingOrchestrator._project_character_names = _project_character_names
WritingOrchestrator._generic_subworld_reference = _generic_subworld_reference
WritingOrchestrator._subworld_role_titles = _subworld_role_titles
WritingOrchestrator._replace_canon_name_strings = _replace_canon_name_strings
WritingOrchestrator._review_event_payload = _review_event_payload
WritingOrchestrator._review_issue_payloads = _review_issue_payloads
WritingOrchestrator._record_map_movement_review_issues = _record_map_movement_review_issues
WritingOrchestrator._review_canon_risk = _review_canon_risk
WritingOrchestrator._load_json_list = _load_json_list
WritingOrchestrator._chapter_plan_snapshot = _chapter_plan_snapshot
WritingOrchestrator._band_plan_snapshot = _band_plan_snapshot
WritingOrchestrator._repair_verification_issue = _repair_verification_issue
WritingOrchestrator._review_with_repair_verification = _review_with_repair_verification
WritingOrchestrator._repair_policy_requested_scope = _repair_policy_requested_scope
WritingOrchestrator._review_has_structural_repair_issue = _review_has_structural_repair_issue
WritingOrchestrator._review_and_maybe_rewrite = _review_and_maybe_rewrite
WritingOrchestrator._review_meta_json = _review_meta_json
WritingOrchestrator._default_repair_instruction = _default_repair_instruction
WritingOrchestrator._apply_repair_patch = _apply_repair_patch
WritingOrchestrator._replace_band_schedule = _replace_band_schedule
WritingOrchestrator._structure_data_from_row = _structure_data_from_row
WritingOrchestrator._reader_promise_from_row = _reader_promise_from_row
WritingOrchestrator._current_chapter_repair_experience_plan = _current_chapter_repair_experience_plan
WritingOrchestrator._chapter_experience_patch_payload = _chapter_experience_patch_payload
WritingOrchestrator._countdown_repair_rule_anchors = _countdown_repair_rule_anchors
WritingOrchestrator._band_schedule_patch_payload = _band_schedule_patch_payload
WritingOrchestrator._arc_payoff_patch_payload = _arc_payoff_patch_payload
WritingOrchestrator._run_project_chapters = _run_project_chapters
WritingOrchestrator._write_chapter_with_attention_fallback = _write_chapter_with_attention_fallback
WritingOrchestrator._is_timeout_like = _is_timeout_like
WritingOrchestrator._is_transient_llm_like = _is_transient_llm_like
WritingOrchestrator._transient_retry_delay = _transient_retry_delay
WritingOrchestrator._current_model_identity = _current_model_identity
WritingOrchestrator._audit_operation_id = _audit_operation_id
WritingOrchestrator._drain_llm_attempt_events = _drain_llm_attempt_events
WritingOrchestrator._safe_prompt_trace_attempts = _safe_prompt_trace_attempts
WritingOrchestrator._error_category_from_attempts = _error_category_from_attempts
WritingOrchestrator._diagnostic_kind_for_failure = _diagnostic_kind_for_failure
WritingOrchestrator._record_failure_prompt_trace = _record_failure_prompt_trace
WritingOrchestrator._record_model_fallback_payloads = _record_model_fallback_payloads
WritingOrchestrator._apply_canon_quality_gate = _apply_canon_quality_gate
WritingOrchestrator._run_obligation_form_gate = _run_obligation_form_gate
WritingOrchestrator._prepare_deferred_acceptance_if_needed = _prepare_deferred_acceptance_if_needed
WritingOrchestrator._band_scope_candidates = _band_scope_candidates
WritingOrchestrator._band_row_by_id = _band_row_by_id
WritingOrchestrator._latest_draft_and_review_for_chapter = _latest_draft_and_review_for_chapter
WritingOrchestrator._apply_canon_candidate = _apply_canon_candidate
WritingOrchestrator._prompt_trace_success_summary = _prompt_trace_success_summary
WritingOrchestrator._apply_world_v4_gate = _apply_world_v4_gate
WritingOrchestrator._filter_resolvable_events = _filter_resolvable_events
WritingOrchestrator._filter_resolvable_state_changes = _filter_resolvable_state_changes
WritingOrchestrator._ensure_genesis_canon_seed_entities = _ensure_genesis_canon_seed_entities
WritingOrchestrator._collect_subworld_candidate_names = _collect_subworld_candidate_names
WritingOrchestrator._validate_subworld_admission = _validate_subworld_admission
WritingOrchestrator._run_phase3_pass = _run_phase3_pass
WritingOrchestrator._flush_background_llm_trace = _flush_background_llm_trace
WritingOrchestrator._compile_world_model_after_acceptance = _compile_world_model_after_acceptance
WritingOrchestrator._run_provisional_band_preview = _run_provisional_band_preview
WritingOrchestrator._abort_requested = _abort_requested
WritingOrchestrator._pause_requested = _pause_requested
WritingOrchestrator._paused_result = _paused_result
WritingOrchestrator._cancelled_result = _cancelled_result
WritingOrchestrator._normalize_provisional_verdict = _normalize_provisional_verdict
WritingOrchestrator._should_degrade_provisional_preview = _should_degrade_provisional_preview
WritingOrchestrator._build_provisional_fallback = _build_provisional_fallback
WritingOrchestrator._load_writer_output_from_meta = _load_writer_output_from_meta
WritingOrchestrator._load_review_verdict = _load_review_verdict
WritingOrchestrator._seed_state = _seed_state
for _module in (
    _acceptance_module,
    _finalization_module,
    _governance_module,
    _project_chapters_module,
    _quality_gates_module,
    _repair_loop_module,
    _review_autofix_module,
    _run_control_module,
    _runtime_helpers_module,
    _world_projection_module,
    _writer_attention_module,
):
    _module.WritingOrchestrator = WritingOrchestrator
WritingOrchestrator.__module__ = "forwin.orchestrator.loop"

__all__ = ["WritingOrchestrator"]
