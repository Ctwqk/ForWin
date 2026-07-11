from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from forwin.models.project import ChapterPlan
import json
from forwin.generation.pipeline_core.common import logger
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.experience import (
    BandDelightSchedule,
    ChapterExperiencePlan,
)
from forwin.models.draft import (
    ChapterDraft,
    ChapterReview,
)
from forwin.checker.rules import ContinuityChecker
from forwin.protocol.review import (
    ContinuityIssue,
    RepairInstruction,
)
from forwin.audit.events import DecisionEventType
from forwin.models.audit import DecisionEvent
from forwin.planning.arc_envelope import ProvisionalBandPreview
from forwin.retrieval import RetrievalBroker
from forwin.runtime.policy import RuntimePolicy
from sqlalchemy.orm import Session
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.protocol.writer import WriterOutput
from forwin.protocol.review import FinalResidualDecision
from forwin.review.decision.engine import AutoDecisionEngine
from forwin.review.decision.rules.final_residual import build_final_residual_rules
from forwin.review.decision.rules.repair_v2 import decide_repair_v2
from forwin.review.decision.types import Decision, DecisionInput, PlanLayerHealth
from forwin.review.repair.local_rewrite_executor import LocalRewriteExecutor
from forwin.generation.pipeline_core.repair_budget import repair_word_budget_patch
from forwin.generation.pipeline_core.repair_budget_events import (
    record_repair_body_budget_event,
)
from forwin.generation.pipeline_core.quality_gates import (
    _latest_draft_and_review_for_chapter,
)


@dataclass(frozen=True, slots=True)
class RepairExecution:
    policy: RuntimePolicy
    retrieval_broker: RetrievalBroker
    _save_prompt_trace_payload: Callable[..., str]
    _plan_writer_output_entities: Callable[..., WriterOutput]
    _review_current_output: Callable[..., ReviewVerdict]
    _apply_canon_name_drift_autofix: Callable[..., WriterOutput | None]
    _apply_placeholder_leakage_autofix: Callable[..., WriterOutput | None]
    _persist_draft_and_review: Callable[
        ..., tuple[WriterOutput, ChapterDraft, ChapterReview]
    ]
    _record_decision_event: Callable[..., DecisionEvent]
    _review_event_payload: Callable[..., dict[str, object]]
    _record_map_movement_review_issues: Callable[..., None]
    _pause_requested: Callable[[], bool]
    _record_rule_decision_event: Callable[..., DecisionEvent]
    _chapter_plan_snapshot: Callable[..., dict[str, object]]
    _band_plan_snapshot: Callable[..., dict[str, object]]
    _emit_progress: Callable[..., None]
    _write_chapter_with_attention_fallback: Callable[..., WriterOutput | None]
    _review_with_repair_verification: Callable[..., ReviewVerdict]
    _chapter_experience_patch_payload: Callable[..., dict[str, object]]
    _run_provisional_band_preview: Callable[..., ProvisionalBandPreview | None]
    _replace_band_schedule: Callable[..., None]
    _band_schedule_patch_payload: Callable[..., dict[str, object]]


REVIEW_REPAIR_PHASE = "review_repair"
CANON_REPAIR_PHASE = "canon_repair"


def _attempt_repair_phase(attempt: object) -> str:
    return str(getattr(attempt, "repair_phase", "") or REVIEW_REPAIR_PHASE)


def _attempts_for_repair_phase(
    attempts: list[object],
    repair_phase: str,
) -> list[object]:
    normalized_phase = str(repair_phase or REVIEW_REPAIR_PHASE)
    return [
        attempt
        for attempt in attempts
        if _attempt_repair_phase(attempt) == normalized_phase
    ]


_CANON_SCOPE_TO_REPAIR_SCOPE = {
    "draft": "draft",
    "chapter": "chapter_plan",
    "chapter_plan": "chapter_plan",
    "band": "band_plan",
    "band_plan": "band_plan",
    "arc": "arc_plan",
    "arc_plan": "arc_plan",
    "book": "book_plan",
    "book_plan": "book_plan",
}
_CANON_AUTO_REPAIR_SCOPES = frozenset({"draft", "chapter_plan", "band_plan"})


def _canon_repair_scope(raw_scope: object) -> str:
    return _CANON_SCOPE_TO_REPAIR_SCOPE.get(str(raw_scope or "").strip().lower(), "")


def _canon_repair_scope_can_run(repair_scope: str) -> bool:
    return str(repair_scope or "") in _CANON_AUTO_REPAIR_SCOPES


def _canon_issue_type_for_scope(repair_scope: str) -> str:
    return {
        "draft": "canon_admission_draft_block",
        "chapter_plan": "canon_admission_chapter_plan_block",
        "band_plan": "canon_admission_band_block",
        "arc_plan": "canon_admission_arc_block",
        "book_plan": "canon_admission_book_block",
    }.get(repair_scope, "")


def _review_from_canon_gate_block(gate_result) -> ReviewVerdict:
    repair_scope = _canon_repair_scope(
        getattr(gate_result, "required_repair_scope", "")
    )
    issue_type = _canon_issue_type_for_scope(repair_scope)
    issue = ContinuityIssue(
        rule_name="canon_admission_block",
        severity="error",
        description=str(
            getattr(gate_result, "gate_summary", "") or "canon admission blocked commit"
        ),
        reviewer="canon_quality_gate",
        issue_type=issue_type or "canon_admission_unrouted_block",
        target_scope=repair_scope or "operator",
        evidence_refs=[
            str(ref)
            for ref in getattr(gate_result, "deterministic_issue_refs", []) or []
            if str(ref or "")
        ],
        source_layer="canon_admission",
        blocking_origin="canon_quality_gate",
        blocking=True,
        original_result=gate_result.model_dump(mode="json")
        if hasattr(gate_result, "model_dump")
        else {},
    )
    repair_instruction = None
    if repair_scope in {"draft", "chapter_plan", "band_plan"}:
        repair_instruction = RepairInstruction(
            repair_scope=repair_scope,  # type: ignore[arg-type]
            failure_type="mixed",
            must_fix=[issue.description],
            must_preserve=[],
            scope_reason="canon admission required repair",
            design_patch={
                "canon_required_repair_scope": repair_scope,
                "canon_gate_summary": str(
                    getattr(gate_result, "gate_summary", "") or ""
                ),
            },
            evidence_refs=list(issue.evidence_refs),
        )
    return ReviewVerdict(
        verdict="fail",
        issues=[issue],
        recommended_action="rewrite" if repair_scope else "pause_for_review",
        review_summary=str(
            getattr(gate_result, "gate_summary", "") or "canon admission blocked commit"
        ),
        reviewer_mode="canon_repair",
        repair_instruction=repair_instruction,
        residual_review_issues=[issue],
    )


def _final_residual_from_engine_decision(decision: Decision) -> FinalResidualDecision:
    return FinalResidualDecision(
        decision=str(
            decision.sub_action.get("final_residual_decision")
            or "manual_review_required"
        ),
        forceable=bool(decision.sub_action.get("forceable")),
        reason=str(decision.reason or ""),
        canon_risk=str(decision.sub_action.get("canon_risk") or "high"),
        residual_issues=list(decision.sub_action.get("residual_issues") or []),
        requires_human=bool(decision.sub_action.get("requires_human", True)),
    )


def _review_candidate(
    self: RepairExecution,
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
    current_output = self._plan_writer_output_entities(
        session=session,
        project_id=project_id,
        chapter_number=chapter_plan.chapter_number,
        writer_output=current_output,
    )
    current_review = self._review_current_output(
        repo=repo,
        checker=checker,
        project_id=project_id,
        context=context,
        writer_output=current_output,
    )
    autofixed_output = self._apply_canon_name_drift_autofix(
        current_output, current_review
    )
    if autofixed_output is not None:
        current_output = self._plan_writer_output_entities(
            session=session,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            writer_output=autofixed_output,
        )
        current_review = self._review_current_output(
            repo=repo,
            checker=checker,
            project_id=project_id,
            context=context,
            writer_output=current_output,
        )
    autofixed_output = self._apply_placeholder_leakage_autofix(
        current_output, current_review
    )
    if autofixed_output is not None:
        current_output = self._plan_writer_output_entities(
            session=session,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            writer_output=autofixed_output,
        )
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
    if current_review.verdict != "fail":
        return current_output, current_review, False

    return _run_repair_loop_for_phase(
        self,
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


def _repair_canon_block(
    self: RepairExecution,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    checker: ContinuityChecker,
    project_id: str,
    chapter_plan: ChapterPlan,
    context,
    writer_output: WriterOutput,
    gate_result,
) -> tuple[WriterOutput, ReviewVerdict, bool]:
    latest_draft, _latest_review = _latest_draft_and_review_for_chapter(
        session=session,
        project_id=project_id,
        chapter_number=chapter_plan.chapter_number,
    )
    synthetic_review = _review_from_canon_gate_block(gate_result)
    if latest_draft is None:
        current_output, current_draft, current_review_row = (
            self._persist_draft_and_review(
                session=session,
                updater=updater,
                chapter_plan=chapter_plan,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                writer_output=writer_output,
                review=synthetic_review,
            )
        )
    else:
        current_output = writer_output
        current_draft = latest_draft
        current_review_row = updater.save_review(latest_draft.id, synthetic_review)
    current_review_event = self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_plan.chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
        scope="chapter",
        summary=(
            f"第{chapter_plan.chapter_number}章 canon gate promoted review to fail "
            "for canon_repair."
        ),
        related_object_type="chapter_review",
        related_object_id=current_review_row.id,
        payload=self._review_event_payload(synthetic_review),
    )
    return _run_repair_loop_for_phase(
        self,
        session=session,
        repo=repo,
        updater=updater,
        checker=checker,
        project_id=project_id,
        chapter_plan=chapter_plan,
        current_context=context,
        current_output=current_output,
        current_draft=current_draft,
        current_review=synthetic_review,
        current_review_row=current_review_row,
        current_writer_trace_id="",
        current_review_trace_id="",
        current_review_event=current_review_event,
        repair_phase=CANON_REPAIR_PHASE,
    )


def _run_repair_loop_for_phase(
    self: RepairExecution,
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
        existing_attempts = repo.list_chapter_rewrite_attempts(
            project_id, chapter_plan.chapter_number
        )
        phase_attempts = _attempts_for_repair_phase(existing_attempts, repair_phase)
        repair_v2_input = DecisionInput(
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            review=current_review,
            signals=[],
            open_obligations=[],
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
        self._record_rule_decision_event(
            updater=updater,
            decision=repair_v2_decision,
            decision_input=repair_v2_input,
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
            final_decision = AutoDecisionEngine(build_final_residual_rules()).decide(
                repair_v2_input
            )
            final_residual = _final_residual_from_engine_decision(final_decision)
            force_accept = final_residual.decision == "force_accept"
            current_review = current_review.model_copy(
                update={
                    "verdict": "warn" if force_accept else current_review.verdict,
                    "repair_exhausted": True,
                    "final_residual_decision": final_residual,
                    "residual_review_issues": list(current_review.issues),
                    "forced_accept_applied": force_accept,
                }
            )
            current_review_row.review_meta_json = _review_meta_json(current_review)
            session.add(current_review_row)
            if force_accept:
                if phase_attempts:
                    phase_attempts[-1].forced_accept_applied = True
                    session.add(phase_attempts[-1])
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="audit_action",
                    event_type=DecisionEventType.FORCED_ACCEPT_APPLIED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章通过 final residual policy。",
                    related_object_type="chapter_review",
                    related_object_id=current_review_row.id,
                    parent_event_id=str(current_review_event.id or ""),
                    payload={
                        "canon_risk": final_residual.canon_risk,
                        "reason": final_residual.reason,
                    },
                )
                return current_output, current_review, True
            return current_output, current_review, False

        attempt_no = len(existing_attempts) + 1
        phase_attempt_no = len(phase_attempts) + 1
        repair_model_preference = {
            "preferred_provider_kind": "",
            "preferred_model": "",
        }
        repair_instruction = (
            current_review.repair_instruction
            or _default_repair_instruction(
                repair_scope=repair_scope,
                context=current_context,
                review=current_review,
            )
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
        session.commit()
        (
            design_patch,
            updated_context,
            result_chapter_plan,
            result_band_plan,
            failure_reason,
        ) = _apply_repair_patch(
            self,
            session=session,
            repo=repo,
            project_id=project_id,
            chapter_plan=chapter_plan,
            context=current_context,
            current_output=current_output,
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
            self.policy.review.allows_repair_scope("local")
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
            if (
                local_result.status == "rewritten"
                and local_result.writer_output is not None
            ):
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
                    llm_preferred_provider_kind=repair_model_preference[
                        "preferred_provider_kind"
                    ],
                    llm_preferred_model=repair_model_preference["preferred_model"],
                )
                if self._pause_requested():
                    session.commit()
                    return current_output, current_review, False
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
        rewritten_output = self._plan_writer_output_entities(
            session=session,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            writer_output=rewritten_output,
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
            rewritten_output = self._plan_writer_output_entities(
                session=session,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                writer_output=autofixed_rewritten_output,
            )
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
            rewritten_output = self._plan_writer_output_entities(
                session=session,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                writer_output=autofixed_rewritten_output,
            )
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
        rewritten_output, rewritten_draft, rewritten_review_row = (
            self._persist_draft_and_review(
                session=session,
                updater=updater,
                chapter_plan=chapter_plan,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                writer_output=rewritten_output,
                review=rewritten_review,
            )
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
        record_repair_body_budget_event(
            self,
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            attempt_no=attempt_no,
            repair_scope=repair_scope,
            current_output=current_output,
            rewritten_output=rewritten_output,
            design_patch=design_patch,
            attempt_row=attempt_row,
            parent_event_id=str(repair_result_event.id or ""),
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
    *,
    repair_scope: str,
    context,
    review: ReviewVerdict,
) -> RepairInstruction:
    budget_patch = repair_word_budget_patch(context)
    return RepairInstruction(
        repair_scope=repair_scope,  # type: ignore[arg-type]
        failure_type="mixed",
        must_fix=[
            issue.description for issue in review.issues if issue.severity == "error"
        ],
        must_preserve=[
            context.chapter_plan_title,
            context.chapter_plan_one_line,
            *(context.chapter_goals[:2]),
        ],
        design_patch=budget_patch,
        evidence_refs=[ref for issue in review.issues for ref in issue.evidence_refs],
    )


def _apply_repair_patch(
    self: RepairExecution,
    *,
    session: Session,
    repo: StateRepository,
    project_id: str,
    chapter_plan: ChapterPlan,
    context,
    current_output: WriterOutput,
    repair_scope: str,
    repair_instruction: RepairInstruction,
) -> tuple[dict[str, object], Any, dict[str, object], dict[str, object], str]:
    current_plan = (
        repo.get_chapter_experience_plan(project_id, chapter_plan.chapter_number)
        or ChapterExperiencePlan()
    )
    band_schedule = repo.get_band_experience_plan_for_chapter(
        project_id, chapter_plan.chapter_number
    )
    arc_structure = repo.get_latest_arc_structure_draft(project_id)
    patch = dict(repair_instruction.design_patch)
    patch["repair_scope"] = repair_scope

    if repair_scope == "draft":
        updated_plan = current_plan.model_copy(
            update=self._chapter_experience_patch_payload(
                current_plan, repair_instruction
            )
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
            update=self._chapter_experience_patch_payload(
                current_plan, repair_instruction
            )
        )
        chapter_plan.experience_plan_json = json.dumps(
            updated_plan.model_dump(mode="json"),
            ensure_ascii=False,
        )
        if str(patch.get("chapter_plan_title") or patch.get("title") or "").strip():
            chapter_plan.title = str(
                patch.get("chapter_plan_title") or patch.get("title") or ""
            ).strip()
        if str(
            patch.get("chapter_plan_one_line") or patch.get("one_line") or ""
        ).strip():
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
            chapter_plan.task_contract_json = json.dumps(
                task_contract_patch, ensure_ascii=False
            )
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
            band_row = repo.get_band_row_for_chapter(
                project_id, chapter_plan.chapter_number
            )
            if active_arc is not None and band_row is not None:
                preview_plans = [
                    repo.get_chapter_plan(project_id, number)
                    for number in range(
                        band_row.chapter_start, band_row.chapter_end + 1
                    )
                ]
                preview_plans = [
                    plan
                    for plan in preview_plans
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
                if preview is not None and preview.aggregate_verdict in {
                    "fail",
                    "error",
                }:
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


class RepairService:
    def review_candidate(
        self,
        *,
        execution: RepairExecution,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        checker: ContinuityChecker,
        project_id: str,
        chapter_plan: ChapterPlan,
        context,
        writer_output: WriterOutput,
    ) -> tuple[WriterOutput, ReviewVerdict, bool]:
        return _review_candidate(
            execution,
            session=session,
            repo=repo,
            updater=updater,
            checker=checker,
            project_id=project_id,
            chapter_plan=chapter_plan,
            context=context,
            writer_output=writer_output,
        )

    def repair_canon_block(
        self,
        *,
        execution: RepairExecution,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        checker: ContinuityChecker,
        project_id: str,
        chapter_plan: ChapterPlan,
        context,
        writer_output: WriterOutput,
        gate_result,
    ) -> tuple[WriterOutput, ReviewVerdict, bool]:
        return _repair_canon_block(
            execution,
            session=session,
            repo=repo,
            updater=updater,
            checker=checker,
            project_id=project_id,
            chapter_plan=chapter_plan,
            context=context,
            writer_output=writer_output,
            gate_result=gate_result,
        )


__all__ = ["RepairExecution", "RepairService"]
