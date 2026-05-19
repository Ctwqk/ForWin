from __future__ import annotations
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from forwin.checker.rules import ContinuityChecker
from forwin.governance import DecisionEventType, issue_group_for_issue
from forwin.models.draft import ChapterReview
from forwin.models.project import ChapterPlan
from forwin.protocol.review import RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.canon_quality.active_rule_store import ActiveRulePatch, CanonQualityActiveRuleStore
from forwin.reviewer.repair_loop_detector import RepairLoopDetector, attempt_record_from_history_item
from forwin.reviewer.outcome import ReviewOutcomeRouter, merge_repair_scope, repair_scope_for_outcome
from forwin.reviewer.repair_handlers.active_rules import apply_active_rules_repair
from forwin.reviewer.repair_handlers.subworld import apply_subworld_admission_repairs
from forwin.reviewer.repair_scope_router import RepairScopeKind, RoutedSignal, route_review_repair_scopes
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater

if TYPE_CHECKING:
    from forwin.orchestrator.loop import WritingOrchestrator


class ChapterRepairCoordinator:
    def __init__(self, host: WritingOrchestrator) -> None:
        self.host = host

    def review_and_maybe_rewrite(
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
        current_review = self.host._review_current_output(
            repo=repo,
            checker=checker,
            project_id=project_id,
            context=current_context,
            writer_output=current_output,
        )
        current_output, current_draft, current_review_row = self.host._persist_draft_and_review(
            session=session,
            updater=updater,
            chapter_plan=chapter_plan,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            writer_output=current_output,
            review=current_review,
        )
        current_review_event = self._record_review_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            review=current_review,
            review_row=current_review_row,
        )
        if current_review.verdict != "fail" or self.host.config.operation_mode == "checkpoint":
            return current_output, current_review, False

        max_attempts = max(1, min(3, int(self.host.config.review_fail_max_rewrites or 3)))
        attempt_history: list[dict[str, object]] = []
        for attempt_no in range(1, max_attempts + 1):
            repair_instruction = self._instruction_for_attempt(
                repo=repo,
                context=current_context,
                writer_output=current_output,
                review=current_review,
                review_row=current_review_row,
                attempt_no=attempt_no,
                attempt_history=attempt_history,
            )
            current_review = current_review.model_copy(update={"repair_instruction": repair_instruction})
            current_review_row.review_meta_json = self.host._review_meta_json(current_review)
            session.add(current_review_row)
            repair_scope = repair_instruction.repair_scope
            if repair_scope in {RepairScopeKind.SUBWORLD.value, RepairScopeKind.ACTIVE_RULES.value}:
                metadata_result = self._run_metadata_repair(
                    session=session,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    repair_instruction=repair_instruction,
                )
                attempt_history.append(
                    {
                        "attempt_no": attempt_no,
                        "repair_scope": repair_scope,
                        "result_verdict": "pass" if metadata_result.get("applied") else "fail",
                        "routed_signals": _routed_signals_from_instruction(repair_instruction),
                    }
                )
                if not metadata_result.get("applied"):
                    current_review = current_review.model_copy(
                        update={
                            "repair_instruction": repair_instruction.model_copy(
                                update={
                                    "repair_scope": RepairScopeKind.OPERATOR.value,
                                    "scope_reason": str(metadata_result.get("reason") or "metadata repair could not apply"),
                                }
                            )
                        }
                    )
                    chapter_plan.status = "needs_operator_review"
                    session.add(chapter_plan)
                    return current_output, current_review, False
                current_review = self.host._review_current_output(
                    repo=repo,
                    checker=checker,
                    project_id=project_id,
                    context=current_context,
                    writer_output=current_output,
                )
                if current_review.verdict != "fail":
                    return current_output, current_review, False
                continue
            if repair_scope == RepairScopeKind.OPERATOR.value:
                chapter_plan.status = "needs_operator_review"
                chapter_plan.residual_review_issues_json = self.host._review_meta_json(current_review)
                session.add(chapter_plan)
                self.host._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.REPAIR_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章 repair 转入 operator review。",
                    related_object_type="chapter_review",
                    related_object_id=current_review_row.id,
                    payload={
                        "attempt_no": attempt_no,
                        "repair_scope": repair_scope,
                        "scope_reason": repair_instruction.scope_reason,
                        "operator_review": True,
                    },
                    parent_event_id=str(current_review_event.id or ""),
                )
                return current_output, current_review, False
            repair_started_event = self.host._record_decision_event(
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
                    "scope_reason": repair_instruction.scope_reason,
                },
                parent_event_id=str(current_review_event.id or ""),
            )
            design_patch = self.host._apply_repair_patch(
                session=session,
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
                repair_scope=repair_scope,
                repair_instruction=repair_instruction,
            )
            session.flush()
            updated_context = self.host.retrieval_broker.build_chapter_context(repo, project_id, chapter_plan)
            updated_context = self.host._audit_current_plan_before_write(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_plan=chapter_plan,
                context=updated_context,
                trigger_stage="pre_repair_rewrite",
            )
            try:
                rewritten_output = self.host._write_chapter_with_attention_fallback(
                    context=updated_context,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    updater=updater,
                    paused_chapters=[],
                    frozen_artifacts=[],
                )
            except Exception as exc:  # noqa: BLE001
                attempt_row = updater.save_chapter_rewrite_attempt(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    attempt_no=attempt_no,
                    trigger_review_id=current_review_row.id,
                    repair_scope=repair_scope,
                    design_patch={
                        **design_patch,
                        "rewrite_error": str(exc),
                        "failure_stage": "repair_execution_error",
                    },
                    source_draft_id=current_draft.id,
                    result_draft_id=current_draft.id,
                    result_verdict="fail",
                    forced_accept_applied=False,
                )
                self.host._record_decision_event(
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
                        "failure_stage": "repair_execution_error",
                    },
                    parent_event_id=str(repair_started_event.id or ""),
                )
                attempt_history.append(
                    {
                        "attempt_no": attempt_no,
                        "repair_scope": repair_scope,
                        "result_verdict": "fail",
                        "routed_signals": _routed_signals_from_instruction(repair_instruction),
                    }
                )
                continue
            if rewritten_output is None:
                attempt_row = updater.save_chapter_rewrite_attempt(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    attempt_no=attempt_no,
                    trigger_review_id=current_review_row.id,
                    repair_scope=repair_scope,
                    design_patch={
                        **design_patch,
                        "rewrite_error": "writer-returned-none",
                        "failure_stage": "writer_returned_none",
                    },
                    source_draft_id=current_draft.id,
                    result_draft_id=current_draft.id,
                    result_verdict="fail",
                    forced_accept_applied=False,
                )
                self.host._record_decision_event(
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
                        "failure_stage": "writer_returned_none",
                    },
                    parent_event_id=str(repair_started_event.id or ""),
                )
                attempt_history.append(
                    {
                        "attempt_no": attempt_no,
                        "repair_scope": repair_scope,
                        "result_verdict": "fail",
                        "routed_signals": _routed_signals_from_instruction(repair_instruction),
                    }
                )
                continue
            rewritten_review = self.host._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=updated_context,
                writer_output=rewritten_output,
            )
            rewritten_output, rewritten_draft, rewritten_review_row = self.host._persist_draft_and_review(
                session=session,
                updater=updater,
                chapter_plan=chapter_plan,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                writer_output=rewritten_output,
                review=rewritten_review,
            )
            forced_accept_applied = (
                self.host.config.operation_mode == "blackbox"
                and attempt_no == max_attempts
                and rewritten_review.verdict == "fail"
            )
            attempt_row = updater.save_chapter_rewrite_attempt(
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                attempt_no=attempt_no,
                trigger_review_id=current_review_row.id,
                repair_scope=repair_scope,
                design_patch=design_patch,
                source_draft_id=current_draft.id,
                result_draft_id=rewritten_draft.id,
                result_verdict=rewritten_review.verdict,
                forced_accept_applied=forced_accept_applied,
            )
            repair_result_event = self.host._record_decision_event(
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
                    "failure_stage": (
                        "review_fail_after_attempt"
                        if rewritten_review.verdict == "fail"
                        else ""
                    ),
                },
                parent_event_id=str(repair_started_event.id or ""),
            )
            if forced_accept_applied:
                rewritten_review = rewritten_review.model_copy(update={"forced_accept_applied": True})
                rewritten_review_row.review_meta_json = self.host._review_meta_json(rewritten_review)
                session.add(rewritten_review_row)
                self.host._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="audit_action",
                    event_type=DecisionEventType.FORCED_ACCEPT_APPLIED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章应用 forced accept。",
                    related_object_type="chapter_review",
                    related_object_id=rewritten_review_row.id,
                    parent_event_id=str(repair_result_event.id or ""),
                )
            current_review_event = self._record_review_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                review=rewritten_review,
                review_row=rewritten_review_row,
                parent_event_id=str(repair_result_event.id or ""),
            )
            attempt_history.append(
                {
                    "attempt_no": attempt_no,
                    "repair_scope": repair_scope,
                    "result_verdict": rewritten_review.verdict,
                    "routed_signals": _routed_signals_from_instruction(repair_instruction),
                }
            )
            current_context = updated_context
            current_output = rewritten_output
            current_draft = rewritten_draft
            current_review = rewritten_review
            current_review_row = rewritten_review_row
            if rewritten_review.verdict != "fail":
                return rewritten_output, rewritten_review, False
        return current_output, current_review, bool(current_review.forced_accept_applied)

    def _instruction_for_attempt(
        self,
        *,
        repo: StateRepository,
        context,
        writer_output: WriterOutput,
        review: ReviewVerdict,
        review_row: ChapterReview,
        attempt_no: int,
        attempt_history: list[dict[str, object]],
    ) -> RepairInstruction:
        outcome = ReviewOutcomeRouter().route(
            review=review,
            signals=[],
            attempt_history=attempt_history,
            current_chapter=int(getattr(context, "chapter_number", 0) or 0),
            target_total_chapters=int(getattr(context, "project_target_total_chapters", 0) or 0),
        )

    def _run_metadata_repair(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_plan: ChapterPlan,
        repair_instruction: RepairInstruction,
    ) -> dict[str, object]:
        signals = _routed_signal_objects_from_instruction(repair_instruction)
        if repair_instruction.repair_scope == RepairScopeKind.SUBWORLD.value:
            report = apply_subworld_admission_repairs(
                session=session,
                project_id=project_id,
                chapter_number=int(chapter_plan.chapter_number or 0),
                signals=signals,
            )
            return {
                "applied": report.applied > 0,
                "report": report.__dict__,
                "reason": "; ".join(report.rejection_reasons),
            }
        raw_patches = repair_instruction.design_patch.get("active_rule_patches")
        patches: list[ActiveRulePatch] = []
        if isinstance(raw_patches, list):
            for raw in raw_patches:
                if isinstance(raw, ActiveRulePatch):
                    patches.append(raw)
                elif isinstance(raw, dict):
                    patches.append(ActiveRulePatch.model_validate(raw))
        if not patches:
            return {"applied": False, "reason": "active_rules repair requires active_rule_patches"}
        report = apply_active_rules_repair(
            project_id=project_id,
            chapter_number=int(chapter_plan.chapter_number or 0),
            patches=patches,
            store=CanonQualityActiveRuleStore(session),
        )
        return {
            "applied": report.applied > 0,
            "report": report.__dict__,
            "reason": "; ".join(report.rejection_reasons),
        }
        routed_scopes = route_review_repair_scopes(review)
        loop_result = (
            RepairLoopDetector().detect(
                scope=routed_scopes[0].scope.value,
                signals=routed_scopes[0].signals,
                history=[attempt_record_from_history_item(item) for item in attempt_history],
            )
            if routed_scopes
            else None
        )
        if loop_result is not None and loop_result.loop_detected:
            return RepairInstruction(
                repair_scope=RepairScopeKind.OPERATOR.value,  # type: ignore[arg-type]
                failure_type="mixed",
                must_fix=[],
                must_preserve=[
                    context.chapter_plan_title,
                    context.chapter_plan_one_line,
                    *(context.chapter_goals[:2]),
                ],
                scope_reason=loop_result.reason,
                design_patch={
                    "repair_loop_detected": True,
                    "repair_loop_similarity": loop_result.similarity,
                    "routed_repair_scopes": [
                        {
                            "scope": routed_scopes[0].scope.value,
                            "signals": [
                                {
                                    "kind": signal.kind,
                                    "severity": signal.severity,
                                    "subject_key": signal.subject_key,
                                    "source_signal_id": signal.source_signal_id,
                                }
                                for signal in routed_scopes[0].signals
                            ],
                        }
                    ],
                },
            )
        first_routed_scope = routed_scopes[0].scope.value if routed_scopes else ""
        deterministic_scope = repair_scope_for_outcome(outcome, attempt_no=attempt_no)
        if first_routed_scope:
            deterministic_scope = first_routed_scope
        base_instruction = review.repair_instruction or self.host._default_repair_instruction(
            repair_scope=deterministic_scope,
            context=context,
            review=review,
        )
        requested_instruction = base_instruction
        requested_scope = str(base_instruction.repair_scope or deterministic_scope)
        if attempt_no >= 3:
            choose_repair_escalation = getattr(self.host.review_hub, "choose_repair_escalation", None)
            if callable(choose_repair_escalation):
                escalated = choose_repair_escalation(
                    repo=repo,
                    context=context,
                    writer_output=writer_output,
                    review=review,
                    repair_attempts=attempt_history,
                )
                if escalated.repair_scope in {"band", "arc"}:
                    requested_instruction = escalated
                    requested_scope = str(escalated.repair_scope or requested_scope)
        final_scope, downgrade_reason = merge_repair_scope(
            deterministic_scope=deterministic_scope,
            requested_scope=requested_scope,
            allow_arc=deterministic_scope == "arc",
        )
        if first_routed_scope in {
            RepairScopeKind.OPERATOR.value,
            RepairScopeKind.ACTIVE_RULES.value,
            RepairScopeKind.SUBWORLD.value,
        }:
            final_scope = first_routed_scope
            downgrade_reason = ""
        design_patch = dict(requested_instruction.design_patch)
        if routed_scopes:
            design_patch["routed_repair_scopes"] = [
                {
                    "scope": scope.scope.value,
                    "signals": [
                        {
                            "kind": signal.kind,
                            "severity": signal.severity,
                            "subject_key": signal.subject_key,
                            "source_signal_id": signal.source_signal_id,
                        }
                        for signal in scope.signals
                    ],
                }
                for scope in routed_scopes
            ]
        if downgrade_reason:
            design_patch["downgrade_reason"] = downgrade_reason
        scope_reason = requested_instruction.scope_reason or outcome.reason
        if downgrade_reason:
            scope_reason = f"{scope_reason}; {downgrade_reason}" if scope_reason else downgrade_reason
        elif final_scope != requested_scope or final_scope != deterministic_scope:
            scope_reason = (
                f"{scope_reason}; deterministic_minimum_scope={deterministic_scope}, "
                f"requested_scope={requested_scope}"
                if scope_reason
                else f"deterministic_minimum_scope={deterministic_scope}, requested_scope={requested_scope}"
            )
        return requested_instruction.model_copy(
            update={
                "repair_scope": final_scope,
                "scope_reason": scope_reason,
                "design_patch": design_patch,
            }
        )

    def _record_review_event(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        review: ReviewVerdict,
        review_row: ChapterReview,
        parent_event_id: str = "",
    ):
        return self.host._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
            scope="chapter",
            summary=f"第{chapter_number}章 review verdict: {review.verdict}",
            related_object_type="chapter_review",
            related_object_id=review_row.id,
            payload={
                "verdict": review.verdict,
                "issue_types": [
                    str(getattr(issue, "issue_type", getattr(issue, "rule_name", "")) or "")
                    for issue in review.issues
                ],
                "issue_groups": [
                    str(getattr(issue, "issue_group", "") or issue_group_for_issue(
                        issue_type=str(getattr(issue, "issue_type", "") or ""),
                        rule_name=str(getattr(issue, "rule_name", "") or ""),
                    ))
                    for issue in review.issues
                ],
                "forced_accept_applied": bool(review.forced_accept_applied),
                "scope_reason": (
                    review.repair_instruction.scope_reason
                    if review.repair_instruction is not None
                    else ""
                ),
            },
            parent_event_id=parent_event_id,
        )


def _routed_signals_from_instruction(instruction: RepairInstruction) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    scopes = instruction.design_patch.get("routed_repair_scopes")
    if not isinstance(scopes, list):
        return result
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        if str(scope.get("scope") or "") != str(instruction.repair_scope or ""):
            continue
        raw_signals = scope.get("signals")
        if isinstance(raw_signals, list):
            result.extend(item for item in raw_signals if isinstance(item, dict))
    return result


def _routed_signal_objects_from_instruction(instruction: RepairInstruction) -> list[RoutedSignal]:
    return [
        RoutedSignal(
            kind=str(item.get("kind") or ""),
            severity=str(item.get("severity") or "warning"),
            subject_key=str(item.get("subject_key") or ""),
            description=str(item.get("description") or ""),
            source_signal_id=str(item.get("source_signal_id") or ""),
            source=str(item.get("source") or ""),
            payload=item.get("payload") if isinstance(item.get("payload"), dict) else {},
        )
        for item in _routed_signals_from_instruction(instruction)
    ]
