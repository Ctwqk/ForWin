from __future__ import annotations

from forwin.orchestrator_loop_core.common import *
from forwin.protocol.review import FinalGateDecision
from forwin.review_engine.engine import AutoDecisionEngine
from forwin.review_engine.rules.final_acceptance import build_final_acceptance_rules
from forwin.review_engine.rules.repair_v2 import decide_repair_v2
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth
from forwin.reviser.local_rewrite_executor import LocalRewriteExecutor

REVIEW_REPAIR_PHASE = "review_repair"
CANON_REPAIR_PHASE = "canon_repair"


def _attempt_repair_phase(attempt: object) -> str:
    return str(getattr(attempt, "repair_phase", "") or REVIEW_REPAIR_PHASE)


def _attempts_for_repair_phase(
    attempts: list[object],
    repair_phase: str,
) -> list[object]:
    normalized_phase = str(repair_phase or REVIEW_REPAIR_PHASE)
    return [attempt for attempt in attempts if _attempt_repair_phase(attempt) == normalized_phase]


def _final_gate_from_engine_decision(decision: Decision) -> FinalGateDecision:
    return FinalGateDecision(
        decision=str(decision.sub_action.get("final_gate_decision") or "manual_review_required"),
        forceable=bool(decision.sub_action.get("forceable")),
        reason=str(decision.reason or ""),
        canon_risk=str(decision.sub_action.get("canon_risk") or "high"),
        residual_issues=list(decision.sub_action.get("residual_issues") or []),
        requires_human=bool(decision.sub_action.get("requires_human", True)),
    )


def _review_and_maybe_rewrite(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    checker: ContinuityChecker,
    project_id: str,
    chapter_plan: ChapterPlan,
    context,
    writer_output: WriterOutput,
) -> tuple[WriterOutput, ReviewVerdict, bool]:
    current_context = context
    current_output = writer_output
    current_writer_trace_id = self._save_prompt_trace_payload(
        session=session,
        updater=updater,
        project_id=project_id,
        prompt_trace=(
            current_output.generation_meta.get("prompt_trace")
            if isinstance(current_output.generation_meta, dict)
            else {}
        ),
    )
    current_review = self._review_current_output(
        repo=repo,
        checker=checker,
        project_id=project_id,
        context=context,
        writer_output=current_output,
    )
    autofixed_output = self._apply_canon_name_drift_autofix(current_output, current_review)
    if autofixed_output is not None:
        current_output = autofixed_output
        current_review = self._review_current_output(
            repo=repo,
            checker=checker,
            project_id=project_id,
            context=context,
            writer_output=current_output,
        )
    protected_subworld_names = self._project_character_names(repo, project_id)
    autofixed_output = self._apply_subworld_admission_autofix(
        current_output,
        current_review,
        protected_names=protected_subworld_names,
    )
    if autofixed_output is not None:
        current_output = autofixed_output
        current_review = self._review_current_output(
            repo=repo,
            checker=checker,
            project_id=project_id,
            context=context,
            writer_output=current_output,
        )
    autofixed_output = self._apply_placeholder_leakage_autofix(current_output, current_review)
    if autofixed_output is not None:
        current_output = autofixed_output
        current_review = self._review_current_output(
            repo=repo,
            checker=checker,
            project_id=project_id,
            context=context,
            writer_output=current_output,
        )
    current_output, current_draft, current_review_row = self._persist_draft_and_review(
        session=session,
        updater=updater,
        chapter_plan=chapter_plan,
        project_id=project_id,
        chapter_number=chapter_plan.chapter_number,
        writer_output=current_output,
        review=current_review,
    )
    current_review_event = self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_plan.chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
        scope="chapter",
        summary=f"第{chapter_plan.chapter_number}章 review verdict: {current_review.verdict}",
        related_object_type="chapter_review",
        related_object_id=current_review_row.id,
        payload=self._review_event_payload(current_review),
    )
    self._record_map_movement_review_issues(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_plan.chapter_number,
        review=current_review,
        parent_event_id=str(current_review_event.id or ""),
    )
    current_review_trace_id = self._save_prompt_trace_payload(
        session=session,
        updater=updater,
        project_id=project_id,
        prompt_trace=current_review.prompt_trace,
        parent_trace_id=current_writer_trace_id,
        decision_event_id=str(current_review_event.id or ""),
    )
    if current_review.verdict != "fail" or self.config.operation_mode != "blackbox":
        return current_output, current_review, False

    return self._run_repair_loop_for_phase(
        session=session,
        repo=repo,
        updater=updater,
        checker=checker,
        project_id=project_id,
        chapter_plan=chapter_plan,
        current_context=context,
        current_output=current_output,
        current_draft=current_draft,
        current_review=current_review,
        current_review_row=current_review_row,
        current_writer_trace_id=current_writer_trace_id,
        current_review_trace_id=current_review_trace_id,
        current_review_event=current_review_event,
        repair_phase=REVIEW_REPAIR_PHASE,
    )


def _run_repair_loop_for_phase(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    checker: ContinuityChecker,
    project_id: str,
    chapter_plan: ChapterPlan,
    current_context,
    current_output: WriterOutput,
    current_draft: ChapterDraft,
    current_review: ReviewVerdict,
    current_review_row: ChapterReview,
    current_writer_trace_id: str,
    current_review_trace_id: str,
    current_review_event,
    repair_phase: str,
) -> tuple[WriterOutput, ReviewVerdict, bool]:
    while True:
        if self._pause_requested():
            return current_output, current_review, False
        existing_attempts = repo.list_chapter_rewrite_attempts(project_id, chapter_plan.chapter_number)
        phase_attempts = _attempts_for_repair_phase(existing_attempts, repair_phase)
        repair_v2_input = DecisionInput(
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            review=current_review,
            signals=[],
            open_obligations=[],
            operation_mode=self.config.operation_mode,
            attempts_completed=len(phase_attempts),
            prior_scope_history=[
                str(getattr(attempt, "repair_scope", "") or "")
                for attempt in phase_attempts
            ],
            budget=None,
            target_total_chapters=0,
            plan_layer_health=PlanLayerHealth(),
        )
        repair_v2_decision = decide_repair_v2(repair_v2_input)
        repair_scope = str(repair_v2_decision.sub_action.get("scope") or "")
        self._record_engine_decision_event(
            updater=updater,
            decision=repair_v2_decision,
            decision_input=repair_v2_input,
            shadow_mismatch=False,
            live_or_shadow="live",
            baseline_outcome="",
            engine_outcome=repair_scope or str(repair_v2_decision.outcome or ""),
            live_source="engine",
            shadow_source="",
            engine_live=True,
            baseline_shadow_evaluated=False,
            baseline_safety_net_used=False,
            severe_mismatch=False,
            related_object_type="chapter_review",
            related_object_id=current_review_row.id,
            parent_event_id=str(current_review_event.id or ""),
        )
        repair_can_run_locally = repair_v2_decision.outcome in {
            "local_repair",
            "chapter_patch",
            "band_patch",
        }
        if not repair_can_run_locally:
            final_decision = AutoDecisionEngine(build_final_acceptance_rules()).decide(repair_v2_input)
            final_gate = _final_gate_from_engine_decision(final_decision)
            current_review = current_review.model_copy(
                update={
                    "repair_exhausted": True,
                    "final_gate_decision": final_gate,
                    "residual_review_issues": list(current_review.issues),
                    "forced_accept_applied": final_gate.decision == "force_accept",
                }
            )
            current_review_row.review_meta_json = self._review_meta_json(current_review)
            session.add(current_review_row)
            if final_gate.decision == "force_accept":
                if existing_attempts:
                    existing_attempts[-1].forced_accept_applied = True
                    session.add(existing_attempts[-1])
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="audit_action",
                    event_type=DecisionEventType.FORCED_ACCEPT_APPLIED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章通过 final force-accept gate。",
                    related_object_type="chapter_review",
                    related_object_id=current_review_row.id,
                    parent_event_id=str(current_review_event.id or ""),
                    payload={"canon_risk": final_gate.canon_risk, "reason": final_gate.reason},
                )
                return current_output, current_review, True
            return current_output, current_review, False

        attempt_no = len(existing_attempts) + 1
        phase_attempt_no = len(phase_attempts) + 1
        repair_model_preference = {
            "preferred_provider_kind": "",
            "preferred_model": "",
        }
        repair_instruction = current_review.repair_instruction or self._default_repair_instruction(
            repair_scope=repair_scope,
            context=current_context,
            review=current_review,
        )
        source_chapter_plan = self._chapter_plan_snapshot(
            repo=repo,
            project_id=project_id,
            chapter_plan=chapter_plan,
        )
        source_band_plan = self._band_plan_snapshot(
            repo=repo,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
        )
        repair_started_event = self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REPAIR_STARTED,
            scope="chapter",
            summary=f"第{chapter_plan.chapter_number}章启动第 {attempt_no} 次 repair。",
            related_object_type="chapter_review",
            related_object_id=current_review_row.id,
            payload={
                "attempt_no": attempt_no,
                "repair_scope": repair_scope,
                **repair_model_preference,
            },
            parent_event_id=str(current_review_event.id or ""),
        )
        (
            design_patch,
            updated_context,
            result_chapter_plan,
            result_band_plan,
            failure_reason,
        ) = self._apply_repair_patch(
            session=session,
            repo=repo,
            project_id=project_id,
            chapter_plan=chapter_plan,
            context=current_context,
            repair_scope=repair_scope,
            repair_instruction=repair_instruction,
        )
        if any(repair_model_preference.values()):
            design_patch = {
                **design_patch,
                "repair_model_preference": repair_model_preference,
            }
        if failure_reason:
            attempt_row = updater.save_chapter_rewrite_attempt(
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                attempt_no=attempt_no,
                repair_phase=repair_phase,
                phase_attempt_no=phase_attempt_no,
                trigger_review_id=current_review_row.id,
                repair_scope=repair_scope,
                design_patch=design_patch,
                source_draft_id=current_draft.id,
                result_draft_id=current_draft.id,
                result_verdict="fail",
                result_review_id=current_review_row.id,
                failure_reason=failure_reason,
                verification={},
                source_chapter_plan=source_chapter_plan,
                result_chapter_plan=result_chapter_plan,
                source_band_plan=source_band_plan,
                result_band_plan=result_band_plan,
                forced_accept_applied=False,
            )
            chapter_plan.repair_attempt_count = attempt_no
            session.add(chapter_plan)
            current_review_event = self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.REPAIR_FAILED,
                scope="chapter",
                summary=f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 失败。",
                reason=failure_reason,
                related_object_type="chapter_rewrite_attempt",
                related_object_id=attempt_row.id,
                payload={
                    "attempt_no": attempt_no,
                    "repair_scope": repair_scope,
                    **repair_model_preference,
                },
                parent_event_id=str(repair_started_event.id or ""),
            )
            continue

        rewritten_output = None
        if (
            bool(getattr(self.config, "review_engine_local_rewrite_enabled", False))
            and repair_v2_decision.outcome == "local_repair"
        ):
            issue_kind = str(repair_v2_decision.sub_action.get("issue_kind") or "")
            local_result = LocalRewriteExecutor().execute(
                draft=current_output,
                issue_kind=issue_kind,
                signals=[],
                context_pack=current_context,
            )
            design_patch = {
                **design_patch,
                "local_rewrite_status": local_result.status,
                "local_rewrite_mode": local_result.mode,
            }
            if local_result.status == "rewritten" and local_result.writer_output is not None:
                rewritten_output = local_result.writer_output
            elif local_result.status == "needs_writer":
                design_patch = {
                    **design_patch,
                    "local_rewrite_instruction": local_result.instruction,
                }
            elif local_result.status == "unsupported":
                logger.info(
                    "Local rewrite unsupported project=%s chapter=%s issue=%s mode=%s",
                    project_id,
                    chapter_plan.chapter_number,
                    issue_kind,
                    local_result.mode,
                )

        self._emit_progress(
            "stage_changed",
            stage="repairing_chapter",
            project_id=project_id,
            current_chapter=chapter_plan.chapter_number,
        )
        if rewritten_output is None:
            try:
                self._emit_progress(
                    "stage_changed",
                    stage="repairing_chapter",
                    project_id=project_id,
                    current_chapter=chapter_plan.chapter_number,
                )
                rewritten_output = self._write_chapter_with_attention_fallback(
                    context=updated_context,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    updater=updater,
                    paused_chapters=[],
                    frozen_artifacts=[],
                    trace_stage_key="chapter_rewrite",
                    llm_preferred_provider_kind=repair_model_preference["preferred_provider_kind"],
                    llm_preferred_model=repair_model_preference["preferred_model"],
                )
            except Exception as exc:  # noqa: BLE001
                attempt_row = updater.save_chapter_rewrite_attempt(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    attempt_no=attempt_no,
                    repair_phase=repair_phase,
                    phase_attempt_no=phase_attempt_no,
                    trigger_review_id=current_review_row.id,
                    repair_scope=repair_scope,
                    design_patch={**design_patch, "rewrite_error": str(exc)},
                    source_draft_id=current_draft.id,
                    result_draft_id=current_draft.id,
                    result_verdict="fail",
                    result_review_id=current_review_row.id,
                    failure_reason=str(exc),
                    verification={},
                    source_chapter_plan=source_chapter_plan,
                    result_chapter_plan=result_chapter_plan,
                    source_band_plan=source_band_plan,
                    result_band_plan=result_band_plan,
                    forced_accept_applied=False,
                )
                chapter_plan.repair_attempt_count = attempt_no
                session.add(chapter_plan)
                current_review_event = self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.REPAIR_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 失败。",
                    reason=str(exc),
                    related_object_type="chapter_rewrite_attempt",
                    related_object_id=attempt_row.id,
                    payload={
                        "attempt_no": attempt_no,
                        "repair_scope": repair_scope,
                        **repair_model_preference,
                    },
                    parent_event_id=str(repair_started_event.id or ""),
                )
                continue

        if rewritten_output is None:
            attempt_row = updater.save_chapter_rewrite_attempt(
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                attempt_no=attempt_no,
                repair_phase=repair_phase,
                phase_attempt_no=phase_attempt_no,
                trigger_review_id=current_review_row.id,
                repair_scope=repair_scope,
                design_patch={**design_patch, "rewrite_error": "writer-returned-none"},
                source_draft_id=current_draft.id,
                result_draft_id=current_draft.id,
                result_verdict="fail",
                result_review_id=current_review_row.id,
                failure_reason="writer-returned-none",
                verification={},
                source_chapter_plan=source_chapter_plan,
                result_chapter_plan=result_chapter_plan,
                source_band_plan=source_band_plan,
                result_band_plan=result_band_plan,
                forced_accept_applied=False,
            )
            chapter_plan.repair_attempt_count = attempt_no
            session.add(chapter_plan)
            current_review_event = self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.REPAIR_FAILED,
                scope="chapter",
                summary=f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 未产出正文。",
                reason="writer-returned-none",
                related_object_type="chapter_rewrite_attempt",
                related_object_id=attempt_row.id,
                payload={
                    "attempt_no": attempt_no,
                    "repair_scope": repair_scope,
                    **repair_model_preference,
                },
                parent_event_id=str(repair_started_event.id or ""),
            )
            continue
        rewritten_writer_trace_id = self._save_prompt_trace_payload(
            session=session,
            updater=updater,
            project_id=project_id,
            prompt_trace=(
                rewritten_output.generation_meta.get("prompt_trace")
                if isinstance(rewritten_output.generation_meta, dict)
                else {}
            ),
            parent_trace_id=current_review_trace_id,
        )
        self._emit_progress(
            "stage_changed",
            stage="repair_review",
            project_id=project_id,
            current_chapter=chapter_plan.chapter_number,
        )
        rewritten_review = self._review_current_output(
            repo=repo,
            checker=checker,
            project_id=project_id,
            context=updated_context,
            writer_output=rewritten_output,
        )
        autofixed_rewritten_output = self._apply_canon_name_drift_autofix(
            rewritten_output,
            rewritten_review,
        )
        if autofixed_rewritten_output is not None:
            rewritten_output = autofixed_rewritten_output
            rewritten_review = self._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=updated_context,
                writer_output=rewritten_output,
            )
        autofixed_rewritten_output = self._apply_subworld_admission_autofix(
            rewritten_output,
            rewritten_review,
            protected_names=self._project_character_names(repo, project_id),
        )
        if autofixed_rewritten_output is not None:
            rewritten_output = autofixed_rewritten_output
            rewritten_review = self._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=updated_context,
                writer_output=rewritten_output,
            )
        autofixed_rewritten_output = self._apply_placeholder_leakage_autofix(
            rewritten_output,
            rewritten_review,
        )
        if autofixed_rewritten_output is not None:
            rewritten_output = autofixed_rewritten_output
            rewritten_review = self._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=updated_context,
                writer_output=rewritten_output,
            )
        rewritten_review = self._review_with_repair_verification(
            original_output=current_output,
            repaired_output=rewritten_output,
            before_review=current_review,
            review=rewritten_review,
            repair_instruction=repair_instruction,
        )
        rewritten_output, rewritten_draft, rewritten_review_row = self._persist_draft_and_review(
            session=session,
            updater=updater,
            chapter_plan=chapter_plan,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            writer_output=rewritten_output,
            review=rewritten_review,
        )
        attempt_row = updater.save_chapter_rewrite_attempt(
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            attempt_no=attempt_no,
            repair_phase=repair_phase,
            phase_attempt_no=phase_attempt_no,
            trigger_review_id=current_review_row.id,
            repair_scope=repair_scope,
            design_patch=design_patch,
            source_draft_id=current_draft.id,
            result_draft_id=rewritten_draft.id,
            result_verdict=rewritten_review.verdict,
            result_review_id=rewritten_review_row.id,
            failure_reason="",
            verification=(
                rewritten_review.repair_verification.model_dump(mode="json")
                if rewritten_review.repair_verification is not None
                else {}
            ),
            source_chapter_plan=source_chapter_plan,
            result_chapter_plan=result_chapter_plan,
            source_band_plan=source_band_plan,
            result_band_plan=result_band_plan,
            forced_accept_applied=False,
        )
        chapter_plan.repair_attempt_count = attempt_no
        session.add(chapter_plan)
        repair_result_event = self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            event_family="evaluation_verdict",
            event_type=(
                DecisionEventType.REPAIR_SUCCEEDED
                if rewritten_review.verdict != "fail"
                else DecisionEventType.REPAIR_FAILED
            ),
            scope="chapter",
            summary=(
                f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 已修复。"
                if rewritten_review.verdict != "fail"
                else f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 仍未通过。"
            ),
            related_object_type="chapter_rewrite_attempt",
            related_object_id=attempt_row.id,
            payload={
                "attempt_no": attempt_no,
                "repair_scope": repair_scope,
                "verdict": rewritten_review.verdict,
            },
            parent_event_id=str(repair_started_event.id or ""),
        )
        current_review_event = self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
            scope="chapter",
            summary=f"第{chapter_plan.chapter_number}章 rewrite 后 verdict: {rewritten_review.verdict}",
            related_object_type="chapter_review",
            related_object_id=rewritten_review_row.id,
            payload=self._review_event_payload(rewritten_review),
            parent_event_id=str(repair_result_event.id or ""),
        )
        self._record_map_movement_review_issues(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            review=rewritten_review,
            parent_event_id=str(current_review_event.id or ""),
        )
        current_review_trace_id = self._save_prompt_trace_payload(
            session=session,
            updater=updater,
            project_id=project_id,
            prompt_trace=rewritten_review.prompt_trace,
            parent_trace_id=rewritten_writer_trace_id,
            decision_event_id=str(current_review_event.id or ""),
        )
        current_context = updated_context
        current_output = rewritten_output
        current_draft = rewritten_draft
        current_review = rewritten_review
        current_review_row = rewritten_review_row
        current_writer_trace_id = rewritten_writer_trace_id
        if rewritten_review.verdict != "fail":
            return rewritten_output, rewritten_review, False

@staticmethod
def _review_meta_json(review: ReviewVerdict) -> str:
    review_meta = review.model_dump(mode="json", exclude_none=True)
    review_meta.pop("verdict", None)
    review_meta.pop("issues", None)
    return json.dumps(review_meta, ensure_ascii=False)

def _default_repair_instruction(
    self,
    *,
    repair_scope: str,
    context,
    review: ReviewVerdict,
) -> RepairInstruction:
    return RepairInstruction(
        repair_scope=repair_scope,  # type: ignore[arg-type]
        failure_type="mixed",
        must_fix=[issue.description for issue in review.issues if issue.severity == "error"],
        must_preserve=[
            context.chapter_plan_title,
            context.chapter_plan_one_line,
            *(context.chapter_goals[:2]),
        ],
        design_patch={},
        evidence_refs=[ref for issue in review.issues for ref in issue.evidence_refs],
    )

def _apply_repair_patch(
    self,
    *,
    session: Session,
    repo: StateRepository,
    project_id: str,
    chapter_plan: ChapterPlan,
    context,
    repair_scope: str,
    repair_instruction: RepairInstruction,
) -> tuple[dict[str, object], Any, dict[str, object], dict[str, object], str]:
    current_plan = repo.get_chapter_experience_plan(project_id, chapter_plan.chapter_number) or ChapterExperiencePlan()
    band_schedule = repo.get_band_experience_plan_for_chapter(project_id, chapter_plan.chapter_number)
    arc_structure = repo.get_latest_arc_structure_draft(project_id)
    patch = dict(repair_instruction.design_patch)
    patch["repair_scope"] = repair_scope

    if repair_scope == "draft":
        updated_plan = current_plan.model_copy(
            update=self._chapter_experience_patch_payload(current_plan, repair_instruction)
        )
        updated_context = context.model_copy(
            update={"chapter_experience_plan": updated_plan}
        )
        return (
            updated_plan.model_dump(mode="json"),
            updated_context,
            self._chapter_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
                experience_plan=updated_plan,
                transient_overlay=True,
            ),
            self._band_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                schedule=band_schedule,
                transient_overlay=True,
            ),
            "",
        )

    if repair_scope == "chapter_plan":
        updated_plan = current_plan.model_copy(
            update=self._chapter_experience_patch_payload(current_plan, repair_instruction)
        )
        chapter_plan.experience_plan_json = json.dumps(
            updated_plan.model_dump(mode="json"),
            ensure_ascii=False,
        )
        if str(patch.get("chapter_plan_title") or patch.get("title") or "").strip():
            chapter_plan.title = str(patch.get("chapter_plan_title") or patch.get("title") or "").strip()
        if str(patch.get("chapter_plan_one_line") or patch.get("one_line") or "").strip():
            chapter_plan.one_line = str(
                patch.get("chapter_plan_one_line") or patch.get("one_line") or ""
            ).strip()
        goal_patch = patch.get("chapter_goals")
        if not isinstance(goal_patch, list):
            goal_patch = patch.get("goals")
        if isinstance(goal_patch, list):
            chapter_plan.goals_json = json.dumps(goal_patch, ensure_ascii=False)
        task_contract_patch = patch.get("chapter_task_contract")
        if not isinstance(task_contract_patch, list):
            task_contract_patch = patch.get("task_contract")
        if isinstance(task_contract_patch, list):
            chapter_plan.task_contract_json = json.dumps(task_contract_patch, ensure_ascii=False)
        session.add(chapter_plan)
        session.flush()
        return (
            updated_plan.model_dump(mode="json"),
            self.retrieval_broker.build_chapter_context(repo, project_id, chapter_plan),
            self._chapter_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
            ),
            self._band_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
            ),
            "",
        )

    if band_schedule is not None:
        updated_schedule = BandDelightSchedule.model_validate(
            self._band_schedule_patch_payload(band_schedule, repair_instruction)
        )
        with session.begin_nested() as nested:
            self._replace_band_schedule(
                session=session,
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                schedule=updated_schedule,
                arc_structure=arc_structure,
                repair_instruction=repair_instruction,
            )
            session.flush()
            transient_chapter_plan = self._chapter_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
                transient_overlay=True,
            )
            transient_band_plan = self._band_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                schedule=updated_schedule,
                transient_overlay=True,
            )
            active_arc = repo.get_active_arc_plan(project_id)
            band_row = repo.get_band_row_for_chapter(project_id, chapter_plan.chapter_number)
            if active_arc is not None and band_row is not None:
                preview_plans = [
                    repo.get_chapter_plan(project_id, number)
                    for number in range(band_row.chapter_start, band_row.chapter_end + 1)
                ]
                preview_plans = [
                    plan for plan in preview_plans
                    if plan is not None and str(plan.status or "") != "accepted"
                ]
                preview = self._run_provisional_band_preview(
                    session=session,
                    project_id=project_id,
                    arc_id=active_arc.id,
                    band_id=band_row.band_id,
                    chapter_plans=preview_plans,
                    persist_result=False,
                )
                if preview is not None and preview.aggregate_verdict in {"fail", "error"}:
                    nested.rollback()
                    session.expire_all()
                    return (
                        updated_schedule.model_dump(mode="json"),
                        context,
                        transient_chapter_plan,
                        transient_band_plan,
                        f"lightweight-provisional:{preview.aggregate_verdict}",
                    )
        return (
            updated_schedule.model_dump(mode="json"),
            self.retrieval_broker.build_chapter_context(repo, project_id, chapter_plan),
            self._chapter_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
            ),
            self._band_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
            ),
            "",
        )

    updated_plan = current_plan.model_copy(
        update=self._chapter_experience_patch_payload(current_plan, repair_instruction)
    )
    updated_context = context.model_copy(update={"chapter_experience_plan": updated_plan})
    return (
        updated_plan.model_dump(mode="json"),
        updated_context,
        self._chapter_plan_snapshot(
            repo=repo,
            project_id=project_id,
            chapter_plan=chapter_plan,
            experience_plan=updated_plan,
            transient_overlay=True,
        ),
        self._band_plan_snapshot(
            repo=repo,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            schedule=band_schedule,
            transient_overlay=True,
        ),
        "",
    )

def _replace_band_schedule(
    self,
    *,
    session: Session,
    repo: StateRepository,
    project_id: str,
    chapter_number: int,
    schedule: BandDelightSchedule,
    arc_structure: ArcStructureDraft | None,
    repair_instruction: RepairInstruction | None = None,
) -> None:
    active_arc = repo.get_active_arc_plan(project_id)
    if active_arc is None:
        return
    session.query(BandExperiencePlan).filter(
        BandExperiencePlan.project_id == project_id,
        BandExperiencePlan.arc_id == active_arc.id,
        BandExperiencePlan.band_id == schedule.band_id,
    ).delete(synchronize_session=False)
    session.add(
        BandExperiencePlan(
            id=new_id(),
            project_id=project_id,
            arc_id=active_arc.id,
            band_id=schedule.band_id,
            chapter_start=schedule.chapter_start,
            chapter_end=schedule.chapter_end,
            stall_guard_max_gap=schedule.stall_guard_max_gap,
            schedule_json=json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False),
        )
    )
    structure_data = self._structure_data_from_row(arc_structure)
    for number in range(max(chapter_number, schedule.chapter_start), schedule.chapter_end + 1):
        plan = repo.get_chapter_plan(project_id, number)
        if plan is None:
            continue
        experience_plan = self.arc_envelope_manager._derive_chapter_experience_plan(
            chapter_number=number,
            structure=structure_data,
            schedule=schedule,
            chapter_plan=plan,
        )
        if number == chapter_number and repair_instruction is not None:
            experience_plan = self._current_chapter_repair_experience_plan(
                experience_plan,
                repair_instruction,
            )
        plan.experience_plan_json = json.dumps(experience_plan.model_dump(mode="json"), ensure_ascii=False)
        session.add(plan)

@classmethod
def _structure_data_from_row(cls, arc_structure: ArcStructureDraft | None):
    from forwin.orchestrator.phase24 import ArcStructureDraftData
    from forwin.protocol.experience import ReaderPromise

    if arc_structure is None:
        return ArcStructureDraftData(
            phase_layout=[],
            key_beats=[],
            thread_priorities=[],
            hotspot_candidates=[],
            compression_candidates=[],
            reader_promise=ReaderPromise(),
            arc_payoff_map=ArcPayoffMap(),
        )
    return ArcStructureDraftData(
        phase_layout=json.loads(arc_structure.phase_layout_json or "[]") or [],
        key_beats=json.loads(arc_structure.key_beats_json or "[]") or [],
        thread_priorities=json.loads(arc_structure.thread_priorities_json or "[]") or [],
        hotspot_candidates=json.loads(arc_structure.hotspot_candidates_json or "[]") or [],
        compression_candidates=json.loads(arc_structure.compression_candidates_json or "[]") or [],
        reader_promise=cls._reader_promise_from_row(arc_structure),
        arc_payoff_map=ArcPayoffMap.model_validate(json.loads(arc_structure.arc_payoff_map_json or "{}") or {}),
    )

@staticmethod
def _reader_promise_from_row(arc_structure: ArcStructureDraft):
    from forwin.protocol.experience import ReaderPromise

    return ReaderPromise.model_validate(json.loads(arc_structure.reader_promise_json or "{}") or {})

@staticmethod
def _current_chapter_repair_experience_plan(
    current_plan: ChapterExperiencePlan,
    repair_instruction: RepairInstruction,
) -> ChapterExperiencePlan:
    return current_plan.model_copy(
        update=WritingOrchestrator._chapter_experience_patch_payload(
            current_plan,
            repair_instruction,
        )
    )

@staticmethod
def _chapter_experience_patch_payload(
    current_plan: ChapterExperiencePlan,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    repair_rule_anchors = WritingOrchestrator._countdown_repair_rule_anchors(repair_instruction.must_fix)
    repair_rule_anchors.extend([
        f"repair must fix: {item}"
        for item in repair_instruction.must_fix[:3]
        if str(item or "").strip()
    ])
    update: dict[str, object] = {
        "planned_reward_tags": list(
            repair_instruction.design_patch.get("planned_reward_tags")
            or current_plan.planned_reward_tags
            or ["mystery"]
        ),
        "selected_template_ids": list(
            repair_instruction.design_patch.get("selected_template_ids")
            or current_plan.selected_template_ids
        ),
        "hook_type": str(
            repair_instruction.design_patch.get("hook_type")
            or current_plan.hook_type
            or "cliffhanger_question"
        ),
        "question_hook": str(
            repair_instruction.design_patch.get("question_hook")
            or current_plan.question_hook
        ),
        "question_resolution": str(
            repair_instruction.design_patch.get("question_resolution")
            or current_plan.question_resolution
        ),
        "immersion_anchors": list(
            repair_instruction.design_patch.get("immersion_anchors")
            or current_plan.immersion_anchors
        ),
        "progress_markers": list(
            repair_instruction.design_patch.get("progress_markers")
            or current_plan.progress_markers
        ),
        "rule_anchors": list(
            repair_instruction.design_patch.get("rule_anchors")
            or current_plan.rule_anchors
        ),
        "relationship_or_status_shift": str(
            repair_instruction.design_patch.get("relationship_or_status_shift")
            or current_plan.relationship_or_status_shift
        ),
        "minimum_progress_channels": list(
            repair_instruction.design_patch.get("minimum_progress_channels")
            or current_plan.minimum_progress_channels
        ),
    }
    if repair_instruction.failure_type == "hook_failure" and "hook_type" not in repair_instruction.design_patch:
        update["hook_type"] = "hard_cliffhanger"
    if repair_instruction.failure_type == "immersion" and not update["immersion_anchors"]:
        update["immersion_anchors"] = ["补入感官锚点", "让角色即时反应落地"]
    if repair_instruction.failure_type == "immersion" and not update["rule_anchors"]:
        update["rule_anchors"] = ["补清规则边界或代价，防止作者强行感"]
    if repair_rule_anchors:
        existing_rule_anchors = [str(item) for item in update.get("rule_anchors", []) or []]
        update["rule_anchors"] = [*repair_rule_anchors, *existing_rule_anchors]
    if repair_instruction.failure_type == "stall" and not update["progress_markers"]:
        update["progress_markers"] = ["让主目标出现不可逆推进"]
    if repair_instruction.failure_type == "stall" and not update["question_hook"]:
        update["question_hook"] = "补出一个比当前更强的新问题"
    return update

@staticmethod
def _countdown_repair_rule_anchors(must_fix: list[str]) -> list[str]:
    anchors: list[str] = []
    for raw in must_fix:
        item = str(raw or "").strip()
        if not item:
            continue
        if "倒计时" not in item:
            continue
        stale_match = re.search(
            r"回溯旧倒计时为\s*([^，。,；;]+).*?([0-9]+)\s*分钟级别",
            item,
        )
        if stale_match:
            raw_target = str(stale_match.group(1) or "").strip()
            latest = int(stale_match.group(2))
            anchors.append(
                "repair countdown hard constraint: 旧计划/旧摘要时间不得写成前文事实；"
                f"{raw_target}必须删除，或明确改成公开伪数据/误导信息，"
                f"同一记忆重置周期只能写小于等于{latest}分钟。"
                "不得写“系统日志原本还有三天/七天/几小时”来解释当前倒计时。"
            )
            continue
        if not any(marker in item for marker in ("回升", "延长", "non_monotonic", "单调")):
            continue
        match = re.search(r"从\s*([0-9]+)\s*分钟(?:回升|延长)到\s*([^，。,；;]+)", item)
        if match:
            previous = int(match.group(1))
            raw_target = str(match.group(2) or "").strip()
            target_digit = re.search(r"([0-9]+)\s*分钟", raw_target)
            target_constraint = (
                f"{int(target_digit.group(1))}分钟必须改成小于等于{previous}分钟"
                if target_digit
                else f"{raw_target}必须删除或改为小于等于{previous}分钟"
            )
            anchors.append(
                "repair countdown hard constraint: 同一倒计时 ledger 在本章全文必须单调减少；"
                f"{target_constraint}，"
                "并同步修正文中所有相关倒计时、角色判断和摘要。除非正文明确 reset 或 branch clock，"
                "不得在更小剩余时间之后再写更大的剩余时间。"
            )
            continue
        anchors.append(
            "repair countdown hard constraint: 同一倒计时 ledger 在本章全文必须单调减少；"
            "重写前先列出正文所有剩余时间，按出现顺序改成不增加序列。除非正文明确 reset 或 branch clock，"
            "不得在更小剩余时间之后再写更大的剩余时间。"
        )
    return anchors

@staticmethod
def _band_schedule_patch_payload(
    schedule: BandDelightSchedule,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    payload = schedule.model_dump(mode="json")
    payload.update(repair_instruction.design_patch)
    if repair_instruction.failure_type == "stall":
        payload["stall_guard_max_gap"] = 1
    if repair_instruction.failure_type == "immersion" and not payload.get("immersion_anchor_scene_goal"):
        payload["immersion_anchor_scene_goal"] = "每章都落一个可感知现场锚点"
    if repair_instruction.failure_type == "stall" and not payload.get("curiosity_beats"):
        payload["curiosity_beats"] = [
            {
                "chapter_hint": schedule.chapter_start,
                "question_open": "当前局面真正危险在哪里",
                "question_resolve": "先确认一个局部真相",
                "escalated_question": "更大的幕后压力是什么",
            }
        ]
    return payload

@staticmethod
def _arc_payoff_patch_payload(
    payoff_map: ArcPayoffMap,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    payload = payoff_map.model_dump(mode="json")
    patch = dict(repair_instruction.design_patch)
    if "macro_payoffs" in patch:
        payload["macro_payoffs"] = patch["macro_payoffs"]
    if "awe_kit" in patch:
        payload["awe_kit"] = patch["awe_kit"]
    if "revelation_layers" in patch:
        payload["revelation_layers"] = patch["revelation_layers"]
    if "ambiguity_constraints" in patch:
        payload["ambiguity_constraints"] = patch["ambiguity_constraints"]
    if repair_instruction.failure_type == "payoff_miss" and not payload.get("macro_payoffs"):
        payload["macro_payoffs"] = [
            {
                "payoff_id": "repair-payoff-1",
                "category": "mystery",
                "template_id": "mystery-locked-clue",
                "target_chapter_hint": "near-term",
                "setup_requirement": "缩短 setup 到本 band 内",
                "success_signal": "读者感到明确回报已经到账",
            }
        ]
    if repair_instruction.failure_type == "immersion" and not payload.get("ambiguity_constraints"):
        payload["ambiguity_constraints"] = ["关键翻盘必须回指既有规则或线索。"]
    return payload



__all__ = [
    "REVIEW_REPAIR_PHASE",
    "CANON_REPAIR_PHASE",
    "_attempt_repair_phase",
    "_attempts_for_repair_phase",
    "_review_and_maybe_rewrite",
    "_run_repair_loop_for_phase",
    "_review_meta_json",
    "_default_repair_instruction",
    "_apply_repair_patch",
    "_replace_band_schedule",
    "_structure_data_from_row",
    "_reader_promise_from_row",
    "_current_chapter_repair_experience_plan",
    "_chapter_experience_patch_payload",
    "_countdown_repair_rule_anchors",
    "_band_schedule_patch_payload",
    "_arc_payoff_patch_payload",
]
